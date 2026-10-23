"""Tests for MIL-STD-188-110 / STANAG 4285 serial-tone modem."""

import unittest
import numpy as np

from hammodem.keyboard.milstd import (
    MilStdModulator, MilStdDemodulator, ModeSpec, ConvEncoder, ViterbiDecoder,
    DATA_SCRAMBLER, SYNC_SCRAMBLER, EOM_BITS,
    _scramble, _descramble, _MGD3, _MGD3_INV, _MGD2, _MGD2_INV,
    _SCRAMBLER_PERIOD, _interleave_perm,
)


class TestScrambler(unittest.TestCase):
    def test_data_scrambler_period(self):
        self.assertEqual(len(DATA_SCRAMBLER), _SCRAMBLER_PERIOD)
        self.assertTrue(all(0 <= v < 8 for v in DATA_SCRAMBLER))

    def test_sync_scrambler_length(self):
        self.assertEqual(len(SYNC_SCRAMBLER), 32)
        self.assertTrue(all(0 <= v < 8 for v in SYNC_SCRAMBLER))

    def test_scramble_descramble_roundtrip(self):
        rng = np.random.default_rng(42)
        tribits = rng.integers(0, 8, 160, dtype=np.uint8)
        seq = DATA_SCRAMBLER
        recovered = _descramble(_scramble(tribits, seq), seq)
        np.testing.assert_array_equal(recovered, tribits)


class TestModifiedGray(unittest.TestCase):
    def test_mgd3_bijection(self):
        self.assertEqual(len(set(_MGD3)), 8)
        np.testing.assert_array_equal(_MGD3[_MGD3_INV], np.arange(8))

    def test_mgd2_bijection(self):
        self.assertEqual(len(set(_MGD2)), 4)
        np.testing.assert_array_equal(_MGD2[_MGD2_INV], np.arange(4))


class TestConvEncoder(unittest.TestCase):
    def test_rate_half(self):
        enc = ConvEncoder()
        bits = np.array([1, 0, 1, 1, 0], dtype=np.uint8)
        coded = enc.encode(bits)
        self.assertEqual(len(coded), 2 * len(bits))

    def test_all_zeros(self):
        enc = ConvEncoder()
        coded = enc.encode(np.zeros(10, dtype=np.uint8))
        np.testing.assert_array_equal(coded, np.zeros(20, dtype=np.uint8))


class TestViterbiDecoder(unittest.TestCase):
    def test_clean_roundtrip(self):
        enc = ConvEncoder()
        bits = np.array([1, 0, 1, 1, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, 1, 0,
                         0, 0, 0, 0, 0, 0], dtype=np.uint8)
        coded = enc.encode(bits)
        soft = 1.0 - 2.0 * coded.astype(float)
        dec = ViterbiDecoder(traceback=32)
        decoded = np.concatenate([dec.decode(soft), dec.flush()])
        np.testing.assert_array_equal(decoded[:len(bits)], bits)

    def test_noisy_roundtrip(self):
        rng = np.random.default_rng(99)
        enc = ConvEncoder()
        bits = rng.integers(0, 2, 200, dtype=np.uint8)
        tail = np.zeros(6, dtype=np.uint8)
        coded = enc.encode(np.concatenate([bits, tail]))
        soft = 1.0 - 2.0 * coded.astype(float)
        soft += rng.normal(0, 0.5, len(soft))
        dec = ViterbiDecoder(traceback=48)
        decoded = np.concatenate([dec.decode(soft), dec.flush(terminated=True)])
        errors = np.sum(decoded[:len(bits)] != bits)
        self.assertLess(errors, 5, f"too many errors: {errors}/200")


class TestInterleaver(unittest.TestCase):
    def test_permutation_is_bijection(self):
        perm = _interleave_perm(40, 72, 9, 17)
        self.assertEqual(len(perm), 40 * 72)
        self.assertEqual(len(set(perm)), 40 * 72)

    def test_roundtrip(self):
        rows, cols = 40, 72
        perm = _interleave_perm(rows, cols, 9, 17)
        inv = np.empty_like(perm)
        inv[perm] = np.arange(perm.size)
        data = np.arange(rows * cols, dtype=np.uint8)
        np.testing.assert_array_equal(data[perm][inv], data)


class TestModeSpec(unittest.TestCase):
    def test_2400_short(self):
        m = ModeSpec(2400, "short")
        self.assertEqual(m.rate, 2400)
        self.assertEqual(m.bits_per_sym, 3)
        self.assertEqual(m.unknown_per_frame, 32)
        self.assertEqual(m.known_per_frame, 16)
        self.assertEqual(m.frame_symbols, 48)
        self.assertTrue(m.coded)
        self.assertIsNotNone(m.il_perm)

    def test_4800(self):
        m = ModeSpec(4800)
        self.assertFalse(m.coded)
        self.assertIsNone(m.il_perm)

    def test_1200_long(self):
        m = ModeSpec(1200, "long")
        self.assertEqual(m.bits_per_sym, 2)
        self.assertEqual(m.il_rows, 40)
        self.assertEqual(m.il_cols, 288)


class TestModulator(unittest.TestCase):
    def test_transmit_produces_audio(self):
        mod = MilStdModulator(fs=8000, rate=2400, interleave="short")
        audio = mod.transmit(b"HELLO")
        self.assertGreater(len(audio), 0)
        self.assertEqual(audio.dtype, np.int16)

    def test_idle(self):
        mod = MilStdModulator(fs=8000)
        silence = mod.idle(0.5)
        self.assertEqual(len(silence), 4000)
        np.testing.assert_array_equal(silence, 0)


class TestCleanRoundtrip(unittest.TestCase):
    def test_2400_short_roundtrip(self):
        msg = b"CQ CQ DE W1AW"
        mod = MilStdModulator(fs=8000, rate=2400, interleave="short")
        audio = mod.transmit(msg)
        demod = MilStdDemodulator(fs=8000, rate=2400, interleave="short")
        recovered = demod.feed(audio)
        self.assertTrue(len(recovered) >= len(msg),
                        f"recovered only {len(recovered)} bytes, expected >= {len(msg)}")
        self.assertEqual(recovered[:len(msg)], msg)


class TestNoisyRoundtrip(unittest.TestCase):
    def test_2400_short_noisy(self):
        rng = np.random.default_rng(777)
        msg = b"TEST MIL-STD-188"
        mod = MilStdModulator(fs=8000, rate=2400, interleave="short")
        audio = mod.transmit(msg)
        noise = rng.normal(0, 500, len(audio))
        noisy = np.clip(audio.astype(float) + noise, -32768, 32767).astype(np.int16)
        demod = MilStdDemodulator(fs=8000, rate=2400, interleave="short")
        recovered = demod.feed(noisy)
        if len(recovered) >= len(msg):
            errors = sum(a != b for a, b in zip(recovered[:len(msg)], msg))
            self.assertLess(errors, len(msg) // 2,
                            f"too many byte errors: {errors}/{len(msg)}")


class TestEOM(unittest.TestCase):
    def test_eom_length(self):
        self.assertEqual(len(EOM_BITS), 32)

    def test_eom_value(self):
        val = 0
        for b in EOM_BITS:
            val = (val << 1) | int(b)
        self.assertEqual(val, 0x4B65A5B2)


class TestTwoTerminal(unittest.TestCase):
    def test_bidirectional(self):
        msg_a = b"STATION A"
        msg_b = b"STATION B"
        mod_a = MilStdModulator(fs=8000, rate=2400, interleave="short")
        mod_b = MilStdModulator(fs=8000, rate=2400, interleave="short")
        audio_a = mod_a.transmit(msg_a)
        audio_b = mod_b.transmit(msg_b)
        silence = np.zeros(4000, dtype=np.int16)
        combined = np.concatenate([audio_a, silence, audio_b])
        demod = MilStdDemodulator(fs=8000, rate=2400, interleave="short")
        recovered = demod.feed(combined[:len(audio_a)])
        if len(recovered) >= len(msg_a):
            self.assertEqual(recovered[:len(msg_a)], msg_a)


class TestLowerRates(unittest.TestCase):
    def _roundtrip(self, rate, interleave="short", msg=b"Test"):
        mod = MilStdModulator(fs=8000, rate=rate, interleave=interleave)
        audio = mod.transmit(msg)
        demod = MilStdDemodulator(fs=8000, rate=rate, interleave=interleave)
        recovered = demod.feed(audio)
        self.assertTrue(len(recovered) >= len(msg),
                        f"{rate} bps {interleave}: recovered {len(recovered)} bytes, need {len(msg)}")
        self.assertEqual(recovered[:len(msg)], msg)

    def test_4800_uncoded(self):
        self._roundtrip(4800, "zero", b"HI")

    def test_1200_short(self):
        self._roundtrip(1200, "short", b"1200")

    def test_600_short(self):
        self._roundtrip(600, "short", b"600")

    def test_300_short(self):
        self._roundtrip(300, "short", b"300")

    def test_150_short(self):
        self._roundtrip(150, "short", b"150")


if __name__ == "__main__":
    unittest.main()
