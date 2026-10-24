"""CLI dispatch tests: text modes must not build the AX.25 packet TNC."""

import contextlib
import io
import unittest
from unittest.mock import patch

from hammodem import main as main_mod
from hammodem.keyboard import tnc as keyboard_tnc


class _FakeAudio:
    name = "fake-audio"
    frame_samples = 160


class _FakeSerial:
    name = "fake-serial"

    def write(self, data):
        pass

    def close(self):
        pass


class ModemCoverageTest(unittest.TestCase):
    def test_text_modems_are_unique(self):
        self.assertEqual(len(main_mod.TEXT_MODEMS), len(set(main_mod.TEXT_MODEMS)))

    def test_all_non_packet_modes_are_known(self):
        expected = {
            "amtor", "pactor", "pactor2", "pactor3", "clover2", "rtty", "psk31", "psk63",
            "mfsk16", "olivia", "contestia", "dominoex", "thor", "js8", "milstd", "stanag4285",
        }
        self.assertEqual(set(main_mod.TEXT_MODEMS), expected)


class TextModeDispatchTest(unittest.TestCase):
    def _run(self, modem):
        with patch.object(main_mod, "Tnc") as tnc_cls, \
             patch.object(main_mod, "UdpAudio", return_value=_FakeAudio()), \
             patch.object(main_mod, "TcpSerial", return_value=_FakeSerial()), \
             patch.object(keyboard_tnc, "run") as run_kb:
            main_mod.main(["--modem", modem, "--udp-audio", "7401", "--text-tcp", "127.0.0.1:1"])
        return tnc_cls, run_kb

    def test_psk31_does_not_construct_packet_tnc(self):
        tnc_cls, run_kb = self._run("psk31")
        tnc_cls.assert_not_called()
        run_kb.assert_called_once()

    def test_olivia_does_not_construct_packet_tnc(self):
        tnc_cls, run_kb = self._run("olivia")
        tnc_cls.assert_not_called()
        run_kb.assert_called_once()

    def test_hf_keyboard_modes_do_not_construct_packet_tnc(self):
        for modem in ("pactor2", "pactor3", "clover2"):
            with self.subTest(modem=modem):
                tnc_cls, run_kb = self._run(modem)
                tnc_cls.assert_not_called()
                run_kb.assert_called_once()

    def test_missing_text_port_is_a_clean_argparse_error(self):
        with patch.object(main_mod, "Tnc") as tnc_cls, \
             patch.object(main_mod, "UdpAudio", return_value=_FakeAudio()):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as cm:
                    main_mod.main(["--modem", "psk31", "--udp-audio", "7401"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("needs --text-pty or --text-tcp", err.getvalue())
        tnc_cls.assert_not_called()


class PacketModeDispatchTest(unittest.TestCase):
    def test_packet_mode_still_builds_tnc(self):
        with patch.object(main_mod, "Tnc") as tnc_cls, \
             patch.object(main_mod, "UdpAudio", return_value=_FakeAudio()), \
             patch.object(main_mod, "Runtime") as runtime_cls:
            runtime_cls.return_value.run.return_value = None
            main_mod.main(["--modem", "afsk1200", "--udp-audio", "7401"])
        tnc_cls.assert_called_once()
        runtime_cls.assert_called_once()


if __name__ == "__main__":
    unittest.main()
