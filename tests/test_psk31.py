import unittest

import numpy as np

from hammodem.keyboard.psk31 import (
    PskDemodulator, PskModulator, VaricodeDecoder, varicode_encode,
)
from hammodem.keyboard.tnc import KeyboardTnc

MSG = "CQ CQ DE W1AW 599 73\r\n"


class VaricodeTest(unittest.TestCase):
    def test_round_trip(self):
        bits = varicode_encode(MSG)
        dec = VaricodeDecoder()
        text = "".join(dec.bit(b) for b in bits)
        self.assertEqual(text, MSG)

    def test_all_printable(self):
        s = "".join(chr(i) for i in range(32, 127))
        bits = varicode_encode(s)
        dec = VaricodeDecoder()
        text = "".join(dec.bit(b) for b in bits)
        self.assertEqual(text, s)


class PskModemTest(unittest.TestCase):
    def _round_trip(self, baud, msg=MSG, snr_db=None):
        m = PskModulator(baud=baud)
        audio = np.concatenate((m.idle(0.5), m.text(msg).astype(float), m.idle(0.5)))
        if snr_db is not None:
            sig_power = np.mean(audio ** 2) + 1e-10
            noise_power = sig_power / (10 ** (snr_db / 10))
            audio = audio + np.random.default_rng(42).normal(0, np.sqrt(noise_power), len(audio))
        d = PskDemodulator(baud=baud)
        text = ""
        for i in range(0, len(audio), 160):
            text += d.feed(audio[i:i + 160])
        return text

    def test_psk31_clean(self):
        text = self._round_trip(31.25)
        self.assertEqual(text, MSG)

    def test_psk63_clean(self):
        text = self._round_trip(62.5)
        self.assertEqual(text, MSG)

    def test_psk31_with_noise(self):
        text = self._round_trip(31.25, snr_db=12)
        wrong = sum(a != b for a, b in zip(text, MSG)) + abs(len(text) - len(MSG))
        self.assertLessEqual(wrong, 3, text)

    def test_clock_offset(self):
        m = PskModulator(baud=31.25)
        audio = np.concatenate((m.idle(0.5), m.text(MSG).astype(float), m.idle(0.5)))
        n = len(audio)
        for ppm in (-300, 300):
            y = np.interp(np.arange(int(n * (1 - ppm * 1e-6))) * (1 + ppm * 1e-6), np.arange(n), audio)
            d = PskDemodulator(baud=31.25)
            text = ""
            for i in range(0, len(y), 160):
                text += d.feed(y[i:i + 160])
            self.assertEqual(text, MSG, f"ppm={ppm}")

    def test_different_center(self):
        for center in (500, 1000, 1500, 2000):
            m = PskModulator(baud=31.25, center=center)
            audio = np.concatenate((m.idle(0.5), m.text(MSG).astype(float), m.idle(0.5)))
            d = PskDemodulator(baud=31.25, center=center)
            text = ""
            for i in range(0, len(audio), 160):
                text += d.feed(audio[i:i + 160])
            self.assertEqual(text, MSG, f"center={center}")


class PskTerminalTest(unittest.TestCase):
    def test_two_terminals(self):
        got = {"a": b"", "b": b""}
        a = KeyboardTnc("psk31", PskModulator(), PskDemodulator(),
                        write=lambda d: got.__setitem__("a", got["a"] + d))
        b = KeyboardTnc("psk31", PskModulator(), PskDemodulator(),
                        write=lambda d: got.__setitem__("b", got["b"] + d))
        a.feed_host(b"HELLO B\r\n")
        for _ in range(200 * 8):
            b.audio_in(a.audio_out(160))
            a.audio_in(b.audio_out(160))
        self.assertIn(b"HELLO B", got["b"])


if __name__ == "__main__":
    unittest.main()
