import unittest

import numpy as np

from hammodem.keyboard.stanag4285 import (
    SYNC_SEQ, FRAME_SYMBOLS, SYNC_SYMBOLS, DATA_SYMBOLS_PER_FRAME, REF_SYMBOLS_PER_FRAME,
    SOM_BITS, EOM_BITS,
    _INPUT_COMMUTATOR, _OUTPUT_COMMUTATOR_2400, _OUTPUT_COMMUTATOR_NORMAL,
    ConvInterleaver, ConvDeinterleaver,
    Stanag4285Modulator, Stanag4285Demodulator,
)
from hammodem.keyboard.milstd import (
    ConvEncoder, ViterbiDecoder, _scramble, _descramble,
    DATA_SCRAMBLER, _SCRAMBLER_PERIOD,
)


class SyncSequenceTest(unittest.TestCase):
    def test_length(self):
        self.assertEqual(len(SYNC_SEQ), 80)

    def test_bpsk_values(self):
        self.assertTrue(np.all((SYNC_SEQ == 0) | (SYNC_SEQ == 4)))

    def test_lfsr_period(self):
        """The 5-bit LFSR has period 31; 80 symbols span more than two cycles."""
        first_31 = SYNC_SEQ[:31]
        next_31 = SYNC_SEQ[31:62]
        np.testing.assert_array_equal(first_31, next_31)


class FrameStructureTest(unittest.TestCase):
    def test_frame_size(self):
        self.assertEqual(FRAME_SYMBOLS, 256)
        self.assertEqual(SYNC_SYMBOLS + DATA_SYMBOLS_PER_FRAME + REF_SYMBOLS_PER_FRAME, 256)

    def test_data_ref_layout(self):
        self.assertEqual(DATA_SYMBOLS_PER_FRAME, 4 * 32)
        self.assertEqual(REF_SYMBOLS_PER_FRAME, 3 * 16)


class CommutatorTest(unittest.TestCase):
    def test_input_commutator_formula(self):
        for i in range(32):
            self.assertEqual(_INPUT_COMMUTATOR[i], (i * 9) % 32)

    def test_input_commutator_is_permutation(self):
        self.assertEqual(set(_INPUT_COMMUTATOR), set(range(32)))

    def test_output_2400_skips_every_fourth(self):
        self.assertEqual(len(_OUTPUT_COMMUTATOR_2400), 24)
        for v in _OUTPUT_COMMUTATOR_2400:
            self.assertNotEqual(v % 4, 3)

    def test_output_normal_sequential(self):
        np.testing.assert_array_equal(_OUTPUT_COMMUTATOR_NORMAL, np.arange(32))


class SomEomTest(unittest.TestCase):
    def test_som_bits(self):
        val = int("".join(str(b) for b in SOM_BITS), 2)
        self.assertEqual(val, 0x03873C3C)

    def test_eom_bits(self):
        val = int("".join(str(b) for b in EOM_BITS), 2)
        self.assertEqual(val, 0x4B65A5B2)


class ConvInterleaverTest(unittest.TestCase):
    def test_identity_at_k0(self):
        """With k=0 (zero delay) the interleaver is a passthrough."""
        il = ConvInterleaver(0)
        data = np.arange(64, dtype=np.uint8) % 2
        out = il.interleave(data)
        np.testing.assert_array_equal(out, data)

    def test_round_trip_short(self):
        k = 4
        il = ConvInterleaver(k)
        deil = ConvDeinterleaver(k)
        data = np.random.default_rng(42).integers(0, 2, 256).astype(np.uint8)
        interleaved = il.interleave(data)
        flush = il.flush(31 * k * 32)
        all_il = np.concatenate([interleaved, flush])
        soft = 1.0 - 2.0 * all_il.astype(float)
        recovered = deil.deinterleave(soft)
        hard = (recovered < 0).astype(np.uint8)
        offset = 31 * k * 32
        np.testing.assert_array_equal(hard[offset:offset + len(data)], data)

    def test_round_trip_long(self):
        k = 12
        il = ConvInterleaver(k)
        deil = ConvDeinterleaver(k)
        data = np.random.default_rng(99).integers(0, 2, 96).astype(np.uint8)
        interleaved = il.interleave(data)
        flush = il.flush(31 * k * 32)
        all_il = np.concatenate([interleaved, flush])
        soft = 1.0 - 2.0 * all_il.astype(float)
        recovered = deil.deinterleave(soft)
        hard = (recovered < 0).astype(np.uint8)
        offset = 31 * k * 32
        np.testing.assert_array_equal(hard[offset:offset + len(data)], data)


class ScramblerTest(unittest.TestCase):
    def test_scramble_descramble_identity(self):
        seq = DATA_SCRAMBLER[:32]
        data = np.array([0, 1, 2, 3, 4, 5, 6, 7] * 4, dtype=np.uint8)
        scrambled = _scramble(data, seq)
        recovered = _descramble(scrambled, seq)
        np.testing.assert_array_equal(recovered, data)


class ModulatorOutputTest(unittest.TestCase):
    def test_output_shape(self):
        mod = Stanag4285Modulator(fs=8000, rate=2400, interleave="short")
        audio = mod.transmit(b"Test")
        self.assertIsInstance(audio, np.ndarray)
        self.assertEqual(audio.dtype, np.int16)
        self.assertGreater(len(audio), 0)

    def test_idle(self):
        mod = Stanag4285Modulator(fs=8000)
        silence = mod.idle(0.5)
        self.assertEqual(len(silence), 4000)
        self.assertTrue(np.all(silence == 0))


class CleanRoundTripTest(unittest.TestCase):
    def test_2400_short(self):
        msg = b"STANAG 4285 clean round-trip test"
        mod = Stanag4285Modulator(fs=8000, rate=2400, interleave="short")
        demod = Stanag4285Demodulator(fs=8000, rate=2400, interleave="short")
        audio = mod.transmit(msg)
        recovered = demod.feed(audio)
        self.assertEqual(recovered, msg)

    def test_short_message(self):
        msg = b"OK"
        mod = Stanag4285Modulator(fs=8000, rate=2400, interleave="short")
        demod = Stanag4285Demodulator(fs=8000, rate=2400, interleave="short")
        audio = mod.transmit(msg)
        recovered = demod.feed(audio)
        self.assertEqual(recovered, msg)


class NoisyRoundTripTest(unittest.TestCase):
    def test_2400_snr_10db(self):
        rng = np.random.default_rng(123)
        msg = b"STANAG noisy test at 10 dB"
        mod = Stanag4285Modulator(fs=8000, rate=2400, interleave="short")
        demod = Stanag4285Demodulator(fs=8000, rate=2400, interleave="short")
        audio = mod.transmit(msg).astype(float)
        sig_power = np.mean(audio[audio != 0] ** 2)
        snr_db = 10
        noise_power = sig_power / (10 ** (snr_db / 10))
        noisy = audio + rng.normal(0, np.sqrt(noise_power), len(audio))
        recovered = demod.feed(noisy.astype(np.int16))
        self.assertEqual(recovered, msg)


class EomDetectionTest(unittest.TestCase):
    def test_eom_found(self):
        mod = Stanag4285Modulator(fs=8000, rate=2400, interleave="short")
        demod = Stanag4285Demodulator(fs=8000, rate=2400, interleave="short")
        audio = mod.transmit(b"EOM")
        recovered = demod.feed(audio)
        self.assertEqual(recovered, b"EOM")


class UncodeRateTest(unittest.TestCase):
    def test_3600_uncoded(self):
        """3600 bps uncoded: no FEC, no interleaver."""
        msg = b"Hello uncoded 3600"
        mod = Stanag4285Modulator(fs=8000, rate=3600, interleave="short")
        demod = Stanag4285Demodulator(fs=8000, rate=3600, interleave="short")
        audio = mod.transmit(msg)
        recovered = demod.feed(audio)
        self.assertEqual(recovered, msg)


class TwoTerminalTest(unittest.TestCase):
    def test_a_sends_b_receives(self):
        msg_a = b"Station A to B via STANAG 4285"
        mod_a = Stanag4285Modulator(fs=8000, rate=2400, interleave="short")
        demod_b = Stanag4285Demodulator(fs=8000, rate=2400, interleave="short")
        audio = mod_a.transmit(msg_a)
        self.assertEqual(demod_b.feed(audio), msg_a)

    def test_b_sends_a_receives(self):
        msg_b = b"Station B to A via STANAG 4285"
        mod_b = Stanag4285Modulator(fs=8000, rate=2400, interleave="short")
        demod_a = Stanag4285Demodulator(fs=8000, rate=2400, interleave="short")
        audio = mod_b.transmit(msg_b)
        self.assertEqual(demod_a.feed(audio), msg_b)


class InternalSampleRateTest(unittest.TestCase):
    def test_9600hz_direct(self):
        msg = b"Internal rate"
        mod = Stanag4285Modulator(fs=9600, rate=2400, interleave="short")
        demod = Stanag4285Demodulator(fs=9600, rate=2400, interleave="short")
        audio = mod.transmit(msg)
        recovered = demod.feed(audio)
        self.assertEqual(recovered, msg)


if __name__ == "__main__":
    unittest.main()
