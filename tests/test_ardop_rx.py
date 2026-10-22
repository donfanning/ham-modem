import unittest
import wave
from pathlib import Path

import numpy as np

from hammodem.ardop import codec, modulate as m
from hammodem.ardop.demod import Receiver

FIX = Path(__file__).parent / "fixtures" / "ardopcf"


def load(name):
    with wave.open(str(FIX / f"{name}.wav")) as w:
        assert w.getframerate() == 12000
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(float)


class ArdopcfRecordingsTest(unittest.TestCase):
    """Frames transmitted by ardopcf 1.0.4.1.3 (lab/ardop/gen_ref.py) decoded by the native receiver."""

    def test_4fsk_data(self):
        for name, mode in (("fsk200_50S", "4FSK.200.50S.E"), ("fsk500_100S", "4FSK.500.100S.E")):
            (d,) = Receiver().decode_all(load(name))
            self.assertEqual((d.name, d.ok, d.rs_fixed), (mode, True, 0))
            self.assertTrue(d.data.startswith(b"ham-modem ARDOP"))       # 16 bytes per 4FSK.200.50S frame

    def test_control_frames(self):
        (d,) = Receiver().decode_all(load("conreq"))
        self.assertEqual((d.name, d.extra["caller"], d.extra["target"]), ("ConReq500M", "N0CALL", "K1ABC"))
        (d,) = Receiver().decode_all(load("idframe"))
        self.assertEqual((d.name, d.extra["caller"], d.extra["grid"]), ("IDFrame", "N0CALL", "FN31"))
        (d,) = Receiver().decode_all(load("ping"))
        self.assertEqual((d.name, d.extra["target"]), ("Ping", "K1ABC"))


    def test_psk_qam_data(self):
        for name, mode in (("psk4_200_100S", "4PSK.200.100S.E"), ("psk8_500_100", "8PSK.500.100.E"),
                           ("qam16_2000_100", "16QAM.2000.100.E")):
            (d,) = Receiver().decode_all(load(name))
            self.assertEqual((d.name, d.ok), (mode, True))
            self.assertTrue(d.data.startswith(b"ham-modem ARDOP"))

    def test_transmitter_matches_ardopcf_sample_for_sample(self):
        """Re-modulating the decoded bytes reproduces ardopcf's waveform (leader, type, data, trailer, TX filter)
        to within rounding."""
        for name in ("fsk200_50S", "fsk500_100S", "psk4_200_100S", "psk8_500_100", "qam16_2000_100"):
            ref = load(name)
            (d,) = Receiver().decode_all(ref)
            ours = m.modulate(m.encode_fsk_data(d.frame_type, d.data, 0xFF)).astype(float)
            self.assertEqual(len(ours), len(ref), name)
            self.assertLessEqual(np.max(np.abs(ours - ref)), 8, name)


class NativeRoundTripTest(unittest.TestCase):
    def frame(self, enc, noise=0.0, seed=1):
        x = np.concatenate((np.zeros(3000), m.modulate(enc).astype(float), np.zeros(3000)))
        if noise:
            x = x + np.random.default_rng(seed).normal(0, noise, len(x))
        return Receiver(sessions=(0x5A,)).decode_all(x)

    def test_data_session_and_noise(self):
        e, _ = codec.mode_codes("4FSK.500.100")
        (d,) = self.frame(m.encode_fsk_data(e, b"connected-session data", 0x5A), noise=4000)
        self.assertEqual((d.ok, d.session, d.data), (True, 0x5A, b"connected-session data"))

    def test_ack_and_nak(self):
        (d,) = self.frame(m.encode_dataack(84, 0x5A))
        self.assertEqual((d.name, d.session), ("DataACK q23", 0x5A))
        (d,) = self.frame(m.encode_dataack(60, 0x5A, nak=True))
        self.assertEqual(d.name, "DataNAK q11")

    def test_psk_qam_all_modes(self):
        rng = np.random.default_rng(7)
        for info in codec.FRAMES.values():
            if info.mod not in m.BITS:
                continue
            data = rng.integers(0, 256, info.data_len * info.carriers, dtype=np.uint8).tobytes()
            (d,) = self.frame(m.encode_fsk_data(info.code, data, 0x5A), noise=300)
            self.assertEqual((d.name, d.ok, d.session, d.data), (info.name, True, 0x5A, data))

    def test_back_to_back_frames(self):
        e, o = codec.mode_codes("4FSK.200.50S")
        x = np.concatenate([np.concatenate((m.modulate_4fsk(m.encode_fsk_data(t, f"part {i}".encode(), 0xFF)),
                                            np.zeros(1200))) for i, t in enumerate((e, o, e))]).astype(float)
        self.assertEqual([d.data for d in Receiver().decode_all(x)], [b"part 0", b"part 1", b"part 2"])


if __name__ == "__main__":
    unittest.main()
