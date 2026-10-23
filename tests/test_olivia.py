import unittest

import numpy as np

from hammodem.keyboard.olivia import (
    OliviaDecoder, OliviaDemodulator, OliviaEncoder, OliviaModulator,
    _fht, _ifht,
)
from hammodem.keyboard.tnc import KeyboardTnc

MSG = "CQ CQ DE W1AW"


class FhtTest(unittest.TestCase):
    def test_round_trip(self):
        data = [1.0, 0.0, -1.0, 0.5, 0.3, -0.7, 0.9, 0.1]
        orig = list(data)
        _fht(data, 8)
        _ifht(data, 8)
        for a, b in zip(data, orig):
            self.assertAlmostEqual(a, b * 8, places=10)

    def test_impulse(self):
        data = [0.0] * 8
        data[3] = 1.0
        _ifht(data, 8)
        _fht(data, 8)
        for i in range(8):
            expected = 8.0 if i == 3 else 0.0
            self.assertAlmostEqual(data[i], expected, places=10)


class EncoderDecoderTest(unittest.TestCase):
    def test_olivia_round_trip(self):
        enc = OliviaEncoder(bits_per_symbol=5, contestia=False)
        dec = OliviaDecoder(bits_per_symbol=5, contestia=False)
        chars = [ord(c) for c in "HELLO"]
        symbols = enc.encode_block(chars)
        self.assertEqual(len(symbols), enc.symbols_per_block)
        for sym in symbols:
            soft = [0.0] * 5
            mask = 1
            for bit in range(5):
                soft[bit] = -1.0 if sym & mask else 1.0
                mask <<= 1
            result = dec.input_symbol(soft)
        self.assertIsNotNone(result)
        decoded = "".join(result)
        self.assertEqual(decoded, "HELLO")

    def test_contestia_round_trip(self):
        enc = OliviaEncoder(bits_per_symbol=5, contestia=True)
        dec = OliviaDecoder(bits_per_symbol=5, contestia=True)
        chars = [ord(c) for c in "CQ DE"]
        symbols = enc.encode_block(chars)
        for sym in symbols:
            soft = [0.0] * 5
            mask = 1
            for bit in range(5):
                soft[bit] = -1.0 if sym & mask else 1.0
                mask <<= 1
            result = dec.input_symbol(soft)
        self.assertIsNotNone(result)
        decoded = "".join(result)
        self.assertEqual(decoded, "CQ DE")

    def test_printable_ascii(self):
        enc = OliviaEncoder(bits_per_symbol=5, contestia=False)
        for start in range(32, 128, 5):
            chars = [32 + (c - 32) % 95 for c in range(start, start + 5)]
            dec = OliviaDecoder(bits_per_symbol=5, contestia=False)
            symbols = enc.encode_block(chars)
            for sym in symbols:
                soft = [0.0] * 5
                mask = 1
                for bit in range(5):
                    soft[bit] = -1.0 if sym & mask else 1.0
                    mask <<= 1
                result = dec.input_symbol(soft)
            self.assertIsNotNone(result)
            for i, ch in enumerate(result):
                self.assertEqual(ord(ch), chars[i], f"char {chars[i]} mismatch at block starting {start}")


class OliviaModemTest(unittest.TestCase):
    def _round_trip(self, msg=MSG, snr_db=None, contestia=False):
        kw = dict(fs=8000, tones=32, bandwidth=1000.0, center=1500.0, contestia=contestia)
        m = OliviaModulator(**kw)
        audio = np.concatenate((
            m.idle(4.0),
            m.text(msg).astype(float),
            m.idle(4.0),
        ))
        if snr_db is not None:
            sig_power = np.mean(audio.astype(float) ** 2) + 1e-10
            noise_power = sig_power / (10 ** (snr_db / 10))
            audio = audio + np.random.default_rng(42).normal(0, np.sqrt(noise_power), len(audio))
        d = OliviaDemodulator(**kw)
        text = ""
        for i in range(0, len(audio), 160):
            text += d.feed(audio[i:i + 160])
        return text

    def test_olivia_clean(self):
        text = self._round_trip()
        self.assertIn("CQ CQ DE W1AW", text)

    def test_olivia_with_noise(self):
        text = self._round_trip(snr_db=6)
        overlap = sum(a == b for a, b in zip(text, MSG))
        self.assertGreaterEqual(overlap, len(MSG) // 2, text)

    def test_contestia_clean(self):
        text = self._round_trip(msg="CQ DE W1AW", contestia=True)
        self.assertIn("CQ DE W1AW", text)

    def test_two_terminals(self):
        got = {"a": b"", "b": b""}
        kw = dict(fs=8000, tones=32, bandwidth=1000.0, center=1500.0)
        a = KeyboardTnc("olivia", OliviaModulator(**kw), OliviaDemodulator(**kw),
                         write=lambda d: got.__setitem__("a", got["a"] + d),
                         lead=4.0, tail=4.0)
        b = KeyboardTnc("olivia", OliviaModulator(**kw), OliviaDemodulator(**kw),
                         write=lambda d: got.__setitem__("b", got["b"] + d),
                         lead=4.0, tail=4.0)
        a.feed_host(b"HELLO B\r\n")
        for _ in range(3000 * 8):
            b.audio_in(a.audio_out(160))
            a.audio_in(b.audio_out(160))
        self.assertIn(b"HELLO B", got["b"])


if __name__ == "__main__":
    unittest.main()
