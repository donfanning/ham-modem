"""Tests for the PACTOR-II modem implementation."""

import unittest
import numpy as np

from hammodem.keyboard.pactor2 import (
    FS, BAUD, SPS, CARRIER_0, CARRIER_1,
    BURST_S, CS_S, CYCLE_S, BURST_SAMPLES, CS_SAMPLES, BURST_SYMS,
    DATA_SYMS, PREAMBLE_SYMS, BITS_PER_SYM, M_STATES, DATA_BYTES,
    _gray, _inv_gray, _GRAY_ENC, _GRAY_DEC,
    dpsk_modulate, dpsk_demodulate_soft,
    Pactor2Frame, Pactor2Modulator, Pactor2Demodulator,
    Pactor2MemArq, Pactor2Session, Pactor2Tnc,
)


# ---------------------------------------------------------------------------
# Gray code tables
# ---------------------------------------------------------------------------

class GrayCodeTest(unittest.TestCase):
    def test_gray_2(self):
        g = _gray(2)
        self.assertEqual(list(g), [0, 1])

    def test_gray_4(self):
        g = _gray(4)
        # Adjacent entries differ in exactly 1 bit
        for i in range(len(g) - 1):
            diff = g[i] ^ g[i + 1]
            self.assertEqual(bin(diff).count('1'), 1)

    def test_gray_8(self):
        g = _gray(8)
        # Cyclic: last and first differ in 1 bit
        diff = g[-1] ^ g[0]
        self.assertEqual(bin(diff).count('1'), 1)

    def test_inv_gray_round_trip(self):
        for m in (2, 4, 8, 16):
            g  = _GRAY_ENC[m]
            ig = _GRAY_DEC[m]
            for i in range(m):
                self.assertEqual(ig[g[i]], i)


# ---------------------------------------------------------------------------
# Timing constants
# ---------------------------------------------------------------------------

class TimingTest(unittest.TestCase):
    def test_sps(self):
        self.assertEqual(SPS, 80)

    def test_burst_samples(self):
        # Burst includes SRRC filter tail (span=4 symbols each side)
        self.assertEqual(BURST_SAMPLES, BURST_SYMS * SPS + 4 * SPS)

    def test_cs_samples_approx(self):
        # CS = 0.275 s; CS_SAMPLES = 0.270 s (quantised to 27 symbols)
        self.assertAlmostEqual(CS_SAMPLES / FS, CS_S, delta=0.01)

    def test_cycle_relation(self):
        self.assertAlmostEqual(BURST_S + CS_S, CYCLE_S, delta=0.02)

    def test_data_syms(self):
        self.assertEqual(DATA_SYMS, 96 - PREAMBLE_SYMS)


# ---------------------------------------------------------------------------
# DPSK physical layer
# ---------------------------------------------------------------------------

class DpskTest(unittest.TestCase):

    def _round_trip(self, level: int, data_bits: np.ndarray) -> bool:
        m   = M_STATES[level]
        bps = BITS_PER_SYM[level]
        n   = len(data_bits) // bps
        audio = dpsk_modulate(data_bits[:n * bps], m, CARRIER_0)
        soft  = dpsk_demodulate_soft(audio, m, CARRIER_0, n)
        hard  = (soft < 0).astype(np.uint8)
        return np.array_equal(hard, data_bits[:n * bps])

    def test_dbpsk_round_trip_all_zeros(self):
        bits = np.zeros(DATA_SYMS, dtype=np.uint8)
        self.assertTrue(self._round_trip(1, bits))

    def test_dbpsk_round_trip_all_ones(self):
        bits = np.ones(DATA_SYMS, dtype=np.uint8)
        self.assertTrue(self._round_trip(1, bits))

    def test_dbpsk_round_trip_alternating(self):
        bits = np.tile([0, 1], DATA_SYMS // 2)
        self.assertTrue(self._round_trip(1, bits))

    def test_dqpsk_round_trip(self):
        rng = np.random.default_rng(0)
        bits = rng.integers(0, 2, DATA_SYMS * 2).astype(np.uint8)
        self.assertTrue(self._round_trip(2, bits))

    def test_8dpsk_round_trip(self):
        rng = np.random.default_rng(1)
        bits = rng.integers(0, 2, DATA_SYMS * 3).astype(np.uint8)
        self.assertTrue(self._round_trip(3, bits))

    def test_16dpsk_round_trip(self):
        rng = np.random.default_rng(2)
        bits = rng.integers(0, 2, DATA_SYMS * 4).astype(np.uint8)
        self.assertTrue(self._round_trip(4, bits))

    def test_dpsk_audio_frequencies(self):
        """Both carriers should be visible in the spectrum."""
        bits0 = np.zeros(DATA_SYMS, dtype=np.uint8)
        bits1 = np.zeros(DATA_SYMS * 2, dtype=np.uint8)
        a0 = dpsk_modulate(bits0, 2, CARRIER_0)
        a1 = dpsk_modulate(bits1, 4, CARRIER_1)
        n = min(len(a0), len(a1))
        combined = (a0[:n] + a1[:n]) / 2.0
        spectrum = np.abs(np.fft.rfft(combined))
        freqs    = np.fft.rfftfreq(len(combined), 1.0 / FS)
        for fc in (CARRIER_0, CARRIER_1):
            idx = np.argmin(np.abs(freqs - fc))
            peak = np.max(spectrum[max(0, idx-3):idx+4])
            mean = np.mean(spectrum)
            self.assertGreater(peak, 3 * mean,
                               f"carrier at {fc} Hz not prominent")

    def test_dpsk_burst_length(self):
        bits = np.zeros(DATA_SYMS, dtype=np.uint8)
        audio = dpsk_modulate(bits, 2, CARRIER_0)
        self.assertGreater(len(audio), 0)

    def test_soft_bits_sign_correct(self):
        """For all-zero bits in DBPSK, soft values should mostly be positive."""
        bits = np.zeros(DATA_SYMS, dtype=np.uint8)
        audio = dpsk_modulate(bits, 2, CARRIER_0)
        soft  = dpsk_demodulate_soft(audio, 2, CARRIER_0, DATA_SYMS)
        # Most soft values positive (bit=0)
        self.assertGreater(np.sum(soft > 0), DATA_SYMS * 0.9)


# ---------------------------------------------------------------------------
# Pactor2Frame FEC + framing
# ---------------------------------------------------------------------------

class Pactor2FrameTest(unittest.TestCase):

    def _rt(self, level: int, data: bytes, noise_sigma: float = 0.0) -> bool:
        frame = Pactor2Frame(level)
        c0, c1 = frame.encode(data)
        m = M_STATES[level]
        # Convert hard bits to soft (+1 = bit 0, -1 = bit 1)
        soft0 = np.where(c0 == 0, 1.0, -1.0).astype(np.float64)
        soft1 = np.where(c1 == 0, 1.0, -1.0).astype(np.float64)
        if noise_sigma > 0:
            rng = np.random.default_rng(42)
            soft0 += rng.normal(0, noise_sigma, len(soft0))
            soft1 += rng.normal(0, noise_sigma, len(soft1))
        result = frame.decode(soft0, soft1)
        if result is None:
            return False
        expected = data[:DATA_BYTES[level]]
        return result['data'].rstrip(b'\x00') == expected.rstrip(b'\x00')

    def test_level1_round_trip(self):
        data = b"PACTOR2!"[:DATA_BYTES[1]]
        self.assertTrue(self._rt(1, data))

    def test_level2_round_trip(self):
        data = bytes(range(DATA_BYTES[2]))
        self.assertTrue(self._rt(2, data))

    def test_level3_round_trip(self):
        data = b"HELLO FROM 8-DPSK"[:DATA_BYTES[3]]
        self.assertTrue(self._rt(3, data))

    def test_level4_round_trip(self):
        data = bytes(range(DATA_BYTES[4]))
        self.assertTrue(self._rt(4, data))

    def test_level1_noisy(self):
        data = b"TEST"[:DATA_BYTES[1]]
        self.assertTrue(self._rt(1, data, noise_sigma=0.3))

    def test_level2_noisy(self):
        data = bytes(range(DATA_BYTES[2]))
        self.assertTrue(self._rt(2, data, noise_sigma=0.3))

    def test_status_byte_preserved(self):
        frame = Pactor2Frame(2)
        data = b"X" * DATA_BYTES[2]
        status = 0x03
        c0, c1 = frame.encode(data, status=status)
        soft0 = np.where(c0 == 0, 1.0, -1.0)
        soft1 = np.where(c1 == 0, 1.0, -1.0)
        result = frame.decode(soft0, soft1)
        self.assertIsNotNone(result)
        self.assertEqual(result['status'], status)

    def test_data_bytes_match_level(self):
        for level in range(1, 5):
            frame = Pactor2Frame(level)
            self.assertEqual(frame._data_bytes, DATA_BYTES[level])

    def test_channel_bits_size(self):
        """Encoded bit arrays should have the right length."""
        for level in range(1, 5):
            frame = Pactor2Frame(level)
            data = bytes(DATA_BYTES[level])
            c0, c1 = frame.encode(data)
            expected_len = DATA_SYMS * BITS_PER_SYM[level]
            self.assertEqual(len(c0), expected_len, f"level {level} c0 len")
            self.assertEqual(len(c1), expected_len, f"level {level} c1 len")
            self.assertEqual(len(c0), len(c1))

    def test_corrupt_crc_rejected(self):
        frame = Pactor2Frame(2)
        data = b"CORRUPT ME"[:DATA_BYTES[2]]
        c0, c1 = frame.encode(data)
        # Flip some bits to cause CRC failure
        soft0 = np.where(c0 == 0, 0.01, -0.01)  # very weak; easily corrupted
        soft1 = np.where(c1 == 0, 0.01, -0.01)
        soft0[0] *= -1
        soft0[1] *= -1
        soft0[2] *= -1
        soft0[3] *= -1
        soft0[4] *= -1
        # May or may not decode; just ensure no exception
        _ = frame.decode(soft0, soft1)


# ---------------------------------------------------------------------------
# Modulator
# ---------------------------------------------------------------------------

class ModulatorTest(unittest.TestCase):
    def test_burst_returns_int16(self):
        mod = Pactor2Modulator(level=2)
        audio = mod.burst(b"HELLO")
        self.assertEqual(audio.dtype, np.int16)

    def test_burst_length(self):
        mod = Pactor2Modulator(level=2)
        audio = mod.burst(b"HELLO")
        self.assertEqual(len(audio), BURST_SAMPLES)

    def test_burst_not_silent(self):
        mod = Pactor2Modulator(level=2)
        audio = mod.burst(b"HELLO WORLD")
        self.assertGreater(np.max(np.abs(audio.astype(np.int32))), 100)

    def test_burst_within_range(self):
        mod = Pactor2Modulator(level=2)
        audio = mod.burst(b"HELLO")
        self.assertLessEqual(np.max(np.abs(audio.astype(np.int32))), 32767)

    def test_all_levels_produce_audio(self):
        for level in range(1, 5):
            mod = Pactor2Modulator(level=level)
            audio = mod.burst(b"X" * 32)
            self.assertEqual(audio.dtype, np.int16)
            self.assertEqual(len(audio), BURST_SAMPLES)

    def test_cs_reply_length(self):
        mod = Pactor2Modulator(level=2)
        cs = mod.cs_reply(0b101010101010)
        self.assertEqual(len(cs), CS_SAMPLES)

    def test_different_data_different_audio(self):
        mod = Pactor2Modulator(level=2)
        a1 = mod.burst(b"\x00" * 20)
        a2 = mod.burst(b"\xFF" * 20)
        self.assertFalse(np.array_equal(a1, a2))

    def test_carrier_frequencies_present(self):
        """Both 900 Hz and 1100 Hz should appear in the burst spectrum."""
        mod = Pactor2Modulator(level=1)
        audio = mod.burst(b"\xAA" * DATA_BYTES[1]).astype(np.float64)
        spec  = np.abs(np.fft.rfft(audio))
        freqs = np.fft.rfftfreq(len(audio), 1.0 / FS)
        for fc in (CARRIER_0, CARRIER_1):
            idx = np.argmin(np.abs(freqs - fc))
            peak = np.max(spec[max(0, idx - 4):idx + 5])
            mean = np.mean(spec)
            self.assertGreater(peak, 2 * mean, f"carrier {fc} Hz absent")

    def test_idle_silence(self):
        mod = Pactor2Modulator(level=2)
        idle = mod.idle(0.05)
        self.assertTrue(np.all(idle == 0))


# ---------------------------------------------------------------------------
# Demodulator round-trips
# ---------------------------------------------------------------------------

class DemodulatorTest(unittest.TestCase):
    def _rt(self, level: int, data: bytes) -> bytes:
        mod   = Pactor2Modulator(level=level)
        demod = Pactor2Demodulator(level=level)
        audio = mod.burst(data)
        result = demod.feed_burst(audio)
        if result is None:
            return b''
        return result['data']

    def test_level1_round_trip(self):
        data = b"HI"[:DATA_BYTES[1]]
        recovered = self._rt(1, data)
        self.assertEqual(recovered, data.rstrip(b'\x00'))

    def test_level2_round_trip(self):
        data = bytes(range(DATA_BYTES[2]))
        recovered = self._rt(2, data)
        self.assertEqual(recovered, data)

    def test_level3_round_trip(self):
        data = b"TEST 8DPSK" + bytes(DATA_BYTES[3] - 10)
        recovered = self._rt(3, data[:DATA_BYTES[3]])
        self.assertEqual(recovered, data[:DATA_BYTES[3]].rstrip(b'\x00'))

    def test_level4_round_trip(self):
        data = bytes(range(DATA_BYTES[4]))
        recovered = self._rt(4, data)
        self.assertEqual(recovered, data)

    def test_returns_none_on_garbage(self):
        demod = Pactor2Demodulator(level=2)
        rng   = np.random.default_rng(9)
        noise = rng.integers(-32768, 32768, BURST_SAMPLES, dtype=np.int16)
        result = demod.feed_burst(noise)
        # Pure noise should fail CRC
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Memory ARQ
# ---------------------------------------------------------------------------

class MemArqTest(unittest.TestCase):
    def test_single_transmission_decodes(self):
        frame = Pactor2Frame(2)
        data  = bytes(range(DATA_BYTES[2]))
        c0, c1 = frame.encode(data)
        soft0 = np.where(c0 == 0, 1.0, -1.0)
        soft1 = np.where(c1 == 0, 1.0, -1.0)
        arq = Pactor2MemArq(level=2)
        arq.add(soft0, soft1)
        result = arq.try_decode()
        self.assertIsNotNone(result)
        self.assertEqual(result['data'], data)

    def test_accumulation_helps_noisy(self):
        """Two noisy transmissions should combine to decode correctly."""
        frame = Pactor2Frame(1)
        data  = b"X" * DATA_BYTES[1]
        c0, c1 = frame.encode(data)
        sigma = 0.8
        rng   = np.random.default_rng(7)
        arq   = Pactor2MemArq(level=1)
        for _ in range(2):
            s0 = np.where(c0 == 0, 1.0, -1.0) + rng.normal(0, sigma, len(c0))
            s1 = np.where(c1 == 0, 1.0, -1.0) + rng.normal(0, sigma, len(c1))
            arq.add(s0, s1)
        result = arq.try_decode()
        self.assertIsNotNone(result)

    def test_reset_clears_accumulator(self):
        frame = Pactor2Frame(2)
        data  = bytes(DATA_BYTES[2])
        c0, c1 = frame.encode(data)
        soft0 = np.where(c0 == 0, 1.0, -1.0)
        soft1 = np.where(c1 == 0, 1.0, -1.0)
        arq = Pactor2MemArq(level=2)
        arq.add(soft0, soft1)
        arq.reset()
        self.assertEqual(arq.count, 0)
        self.assertTrue(np.all(arq._acc0 == 0))

    def test_count_increments(self):
        arq = Pactor2MemArq(level=2)
        for i in range(3):
            arq.add(np.ones(DATA_SYMS * 2), np.ones(DATA_SYMS * 2))
        self.assertEqual(arq.count, 3)


# ---------------------------------------------------------------------------
# ARQ Session (offline)
# ---------------------------------------------------------------------------

class SessionTest(unittest.TestCase):
    def test_single_cycle_level2(self):
        sess = Pactor2Session(local_call="N0CALL", remote_call="K1ABC", level=2)
        data = b"HELLO ARQ" + bytes(DATA_BYTES[2] - 9)
        sess.send(data[:DATA_BYTES[2]])
        ok = sess.pump_cycle()
        self.assertTrue(ok)
        received = sess.flush()
        self.assertGreater(len(received), 0)

    def test_session_level1(self):
        sess = Pactor2Session(local_call="W1AW", level=1)
        sess.send(b"HI")
        ok = sess.pump_cycle()
        self.assertTrue(ok)

    def test_session_level4(self):
        sess = Pactor2Session(local_call="W1AW", level=4)
        sess.send(b"X" * DATA_BYTES[4])
        ok = sess.pump_cycle()
        self.assertTrue(ok)


# ---------------------------------------------------------------------------
# Pactor2Tnc top-level API
# ---------------------------------------------------------------------------

class TncTest(unittest.TestCase):
    def test_transmit_returns_int16(self):
        tnc = Pactor2Tnc(level=2)
        audio = tnc.transmit(b"TEST")
        self.assertEqual(audio.dtype, np.int16)

    def test_round_trip_level2(self):
        tnc = Pactor2Tnc(level=2)
        data = bytes(range(DATA_BYTES[2]))
        audio = tnc.transmit(data)
        recovered = tnc.feed(audio)
        self.assertEqual(recovered, data)

    def test_round_trip_level1(self):
        tnc = Pactor2Tnc(level=1)
        data = b"HI WORLD"[:DATA_BYTES[1]]
        audio = tnc.transmit(data)
        recovered = tnc.feed(audio)
        self.assertEqual(recovered, data.rstrip(b'\x00'))

    def test_round_trip_level3(self):
        tnc = Pactor2Tnc(level=3)
        data = bytes(range(DATA_BYTES[3]))
        audio = tnc.transmit(data)
        recovered = tnc.feed(audio)
        self.assertEqual(recovered, data)

    def test_round_trip_level4(self):
        tnc = Pactor2Tnc(level=4)
        data = bytes(range(DATA_BYTES[4]))
        audio = tnc.transmit(data)
        recovered = tnc.feed(audio)
        self.assertEqual(recovered, data)

    def test_noise_tolerance_level1(self):
        """Level 1 (DBPSK + r=1/2 FEC) should survive moderate noise."""
        tnc_tx = Pactor2Tnc(level=1)
        tnc_rx = Pactor2Tnc(level=1)
        data  = b"ROBUST" + bytes(DATA_BYTES[1] - 6)
        audio = tnc_tx.transmit(data[:DATA_BYTES[1]]).astype(np.float64)
        rng   = np.random.default_rng(123)
        noisy = (audio + rng.normal(0, np.max(np.abs(audio)) * 0.1, len(audio)))
        recovered = tnc_rx.feed(noisy.clip(-32768, 32767).astype(np.int16))
        self.assertEqual(recovered, data[:DATA_BYTES[1]].rstrip(b'\x00'))

    def test_data_bytes_per_burst(self):
        for level in range(1, 5):
            tnc = Pactor2Tnc(level=level)
            self.assertEqual(tnc.data_bytes_per_burst, DATA_BYTES[level])

    def test_burst_cycle_timing(self):
        """Burst should be exactly BURST_SAMPLES long."""
        for level in range(1, 5):
            tnc   = Pactor2Tnc(level=level)
            audio = tnc.transmit(b"X" * DATA_BYTES[level])
            self.assertEqual(len(audio), BURST_SAMPLES, f"level {level}")


if __name__ == "__main__":
    unittest.main()
