import unittest

import numpy as np

from hammodem.keyboard.mfsk16 import (
    MfskDemodulator, MfskModulator, MfskVaricodeDecoder, ViterbiDecoder,
    conv_encode, mfsk_varicode_encode, Interleaver,
)
from hammodem.keyboard.tnc import KeyboardTnc

MSG = "CQ CQ DE W1AW\r"


class MfskVaricodeTest(unittest.TestCase):
    def test_round_trip(self):
        bits = mfsk_varicode_encode(MSG)
        dec = MfskVaricodeDecoder()
        text = "".join(dec.bit(b) for b in bits) + dec.flush()
        self.assertEqual(text, MSG)

    def test_all_printable(self):
        s = "".join(chr(i) for i in range(32, 127))
        bits = mfsk_varicode_encode(s)
        dec = MfskVaricodeDecoder()
        text = "".join(dec.bit(b) for b in bits) + dec.flush()
        self.assertEqual(text, s)


class FecTest(unittest.TestCase):
    def test_conv_round_trip(self):
        data = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 1, 0, 0, 1]
        encoded = conv_encode(data)
        self.assertEqual(len(encoded), 2 * len(data))
        vit = ViterbiDecoder()
        decoded = []
        for i in range(0, len(encoded), 2):
            b = vit.step(encoded[i], encoded[i + 1])
            if b is not None:
                decoded.append(b)
        decoded.extend(vit.flush())
        self.assertEqual(decoded[:len(data)], data)

    def test_single_bit_error(self):
        data = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 1, 0, 0, 1, 0, 0, 0, 0]
        encoded = conv_encode(data)
        encoded[5] ^= 1  # flip one bit
        vit = ViterbiDecoder()
        decoded = []
        for i in range(0, len(encoded), 2):
            b = vit.step(encoded[i], encoded[i + 1])
            if b is not None:
                decoded.append(b)
        decoded.extend(vit.flush())
        self.assertEqual(decoded[:len(data)], data)


class InterleaveTest(unittest.TestCase):
    def test_round_trip(self):
        fwd = Interleaver(reverse=False)
        rev = Interleaver(reverse=True)
        data = list(range(256))
        recovered = []
        for val in data:
            intlv = fwd.bits(val & 0xF)
            recovered.append(rev.bits(intlv))
        # After the pipeline delay, the data should match
        delay = (4 - 1) * 10  # size-1 * depth delay symbols
        for i in range(delay, len(data)):
            self.assertEqual(recovered[i], data[i - delay] & 0xF,
                             f"mismatch at {i}: got {recovered[i]}, expected {data[i-delay] & 0xF}")


class MfskModemTest(unittest.TestCase):
    def _round_trip(self, msg=MSG, snr_db=None):
        m = MfskModulator()
        audio = np.concatenate((
            m.idle(5.0),
            m.text(msg).astype(float),
            m.idle(5.0),
        ))
        if snr_db is not None:
            sig_power = np.mean(audio.astype(float) ** 2) + 1e-10
            noise_power = sig_power / (10 ** (snr_db / 10))
            audio = audio + np.random.default_rng(42).normal(0, np.sqrt(noise_power), len(audio))
        d = MfskDemodulator()
        text = ""
        for i in range(0, len(audio), 160):
            text += d.feed(audio[i:i + 160])
        return text

    def test_mfsk16_clean(self):
        text = self._round_trip()
        self.assertIn("CQ CQ DE W1AW", text)

    def test_mfsk16_with_noise(self):
        text = self._round_trip(snr_db=10)
        overlap = sum(a == b for a, b in zip(text, MSG))
        self.assertGreaterEqual(overlap, len(MSG) // 2, text)

    def test_two_terminals(self):
        got = {"a": b"", "b": b""}
        a = KeyboardTnc("mfsk16", MfskModulator(), MfskDemodulator(),
                         write=lambda d: got.__setitem__("a", got["a"] + d),
                         lead=5.0, tail=5.0)
        b = KeyboardTnc("mfsk16", MfskModulator(), MfskDemodulator(),
                         write=lambda d: got.__setitem__("b", got["b"] + d),
                         lead=5.0, tail=5.0)
        a.feed_host(b"HELLO B\r\n")
        for _ in range(2000 * 8):
            b.audio_in(a.audio_out(160))
            a.audio_in(b.audio_out(160))
        self.assertIn(b"HELLO B", got["b"])


if __name__ == "__main__":
    unittest.main()
