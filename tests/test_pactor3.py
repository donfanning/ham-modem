"""Tests for the PACTOR-III modem implementation."""

import unittest
from typing import Optional
import numpy as np

from hammodem.keyboard.pactor3 import (
    FS, BAUD, SPS,
    CARRIER_BASE, CARRIER_SPACING, CARRIER_FREQS,
    CARRIER_INDICES, N_CARRIERS, M_PER_LEVEL, BPS_PER_LEVEL,
    DATA_BYTES, _PKT_BYTES, _CHANNEL_BITS, _INFO_BITS,
    BURST_SAMPLES, CS_SAMPLES,
    DATA_SYMS, PREAMBLE_SYMS,
    Pactor3Frame, Pactor3Modulator, Pactor3Demodulator,
    Pactor3MemArq, Pactor3Session, Pactor3Tnc,
)


# ---------------------------------------------------------------------------
# Carrier table
# ---------------------------------------------------------------------------

class CarrierTableTest(unittest.TestCase):

    def test_carrier_freqs_spacing(self):
        for i in range(1, 18):
            self.assertAlmostEqual(
                CARRIER_FREQS[i] - CARRIER_FREQS[i - 1], CARRIER_SPACING
            )

    def test_carrier_base(self):
        self.assertAlmostEqual(CARRIER_FREQS[0], CARRIER_BASE)

    def test_carrier_count_per_level(self):
        expected = {1: 2, 2: 6, 3: 14, 4: 14, 5: 16, 6: 18}
        for lvl, n in expected.items():
            self.assertEqual(N_CARRIERS[lvl], n, f"level {lvl}")

    def test_level1_carriers(self):
        # Carriers 5 and 12 → 1080 Hz and 1920 Hz
        idx = CARRIER_INDICES[1]
        self.assertEqual(idx, [5, 12])
        self.assertAlmostEqual(CARRIER_FREQS[5],  1080.0)
        self.assertAlmostEqual(CARRIER_FREQS[12], 1920.0)

    def test_level6_all_carriers(self):
        # Level 6: all 18 carriers, 480..2520 Hz
        idx = CARRIER_INDICES[6]
        self.assertEqual(len(idx), 18)
        self.assertEqual(idx[0], 0)
        self.assertEqual(idx[-1], 17)
        self.assertAlmostEqual(CARRIER_FREQS[17], 480.0 + 17 * 120.0)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class ConstantsTest(unittest.TestCase):

    def test_channel_bits(self):
        for level in range(1, 7):
            expected = N_CARRIERS[level] * DATA_SYMS * BPS_PER_LEVEL[level]
            self.assertEqual(_CHANNEL_BITS[level], expected, f"level {level}")

    def test_info_bits_half_channel(self):
        for level in range(1, 7):
            self.assertEqual(_INFO_BITS[level], _CHANNEL_BITS[level] // 2)

    def test_data_bytes_positive(self):
        for level in range(1, 7):
            self.assertGreater(DATA_BYTES[level], 0, f"level {level}")

    def test_modulation_levels(self):
        for lvl in (1, 2, 3):
            self.assertEqual(M_PER_LEVEL[lvl], 2)   # DBPSK
        for lvl in (4, 5, 6):
            self.assertEqual(M_PER_LEVEL[lvl], 4)   # DQPSK

    def test_data_bytes_monotone(self):
        # More carriers → more data
        for k in range(1, 6):
            self.assertLessEqual(DATA_BYTES[k], DATA_BYTES[k + 1])


# ---------------------------------------------------------------------------
# Frame: encode/decode
# ---------------------------------------------------------------------------

class FrameTest(unittest.TestCase):

    def _rt(self, level: int, data: bytes, noise: float = 0.0) -> bool:
        frame = Pactor3Frame(level)
        carriers = frame.encode(data)
        softs = []
        rng = np.random.default_rng(level + 10)
        for bits in carriers:
            s = np.where(bits == 0, 1.0, -1.0).astype(np.float64)
            if noise > 0:
                s += rng.normal(0, noise, len(s))
            softs.append(s)
        result = frame.decode(softs)
        if result is None:
            return False
        return result['data'].rstrip(b'\x00') == data.rstrip(b'\x00')

    def test_channel_bits_size(self):
        for level in range(1, 7):
            frame = Pactor3Frame(level)
            data = bytes(DATA_BYTES[level])
            carriers = frame.encode(data)
            self.assertEqual(len(carriers), N_CARRIERS[level])
            expected = DATA_SYMS * BPS_PER_LEVEL[level]
            for i, c in enumerate(carriers):
                self.assertEqual(len(c), expected, f"level {level} carrier {i}")

    def test_level1_round_trip(self):
        data = b"HI"[:DATA_BYTES[1]]
        self.assertTrue(self._rt(1, data))

    def test_level2_round_trip(self):
        data = bytes(range(DATA_BYTES[2]))
        self.assertTrue(self._rt(2, data))

    def test_level3_round_trip(self):
        data = b"P3 14-CARRIER" + bytes(DATA_BYTES[3] - 13)
        self.assertTrue(self._rt(3, data[:DATA_BYTES[3]]))

    def test_level4_round_trip(self):
        data = bytes(range(DATA_BYTES[4]))
        self.assertTrue(self._rt(4, data))

    def test_level5_round_trip(self):
        rng = np.random.default_rng(5)
        data = rng.integers(0, 256, DATA_BYTES[5], dtype=np.uint8).tobytes()
        self.assertTrue(self._rt(5, data))

    def test_level6_round_trip(self):
        rng = np.random.default_rng(6)
        data = rng.integers(0, 256, DATA_BYTES[6], dtype=np.uint8).tobytes()
        self.assertTrue(self._rt(6, data))

    def test_level1_noisy(self):
        data = b"TEST"[:DATA_BYTES[1]]
        self.assertTrue(self._rt(1, data, noise=0.3))

    def test_level2_noisy(self):
        data = bytes(range(DATA_BYTES[2]))
        self.assertTrue(self._rt(2, data, noise=0.3))

    def test_status_preserved(self):
        frame = Pactor3Frame(3)
        data = bytes(DATA_BYTES[3])
        status = 0x05
        carriers = frame.encode(data, status=status)
        softs = [np.where(c == 0, 1.0, -1.0) for c in carriers]
        result = frame.decode(softs)
        self.assertIsNotNone(result)
        self.assertEqual(result['status'], status)

    def test_corrupt_crc_rejected(self):
        frame = Pactor3Frame(2)
        data = b"CORRUPT"[:DATA_BYTES[2]]
        carriers = frame.encode(data)
        # Flip some bits in carrier 0 to break CRC
        carriers[0] = 1 - carriers[0]
        softs = [np.where(c == 0, 1.0, -1.0) for c in carriers]
        result = frame.decode(softs)
        self.assertIsNone(result)

    def test_wrong_carrier_count_rejected(self):
        frame = Pactor3Frame(2)
        data = bytes(DATA_BYTES[2])
        carriers = frame.encode(data)
        softs = [np.where(c == 0, 1.0, -1.0) for c in carriers]
        # Wrong number of carriers
        result = frame.decode(softs[:3])
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Modulator / Demodulator round-trips (audio path)
# ---------------------------------------------------------------------------

class ModDemodTest(unittest.TestCase):

    def _rt(self, level: int, data: bytes) -> Optional[bytes]:
        mod   = Pactor3Modulator(level=level)
        demod = Pactor3Demodulator(level=level)
        audio = mod.burst(data)
        result = demod.feed_burst(audio)
        return None if result is None else result['data']

    def test_burst_dtype(self):
        mod = Pactor3Modulator(level=1)
        audio = mod.burst(b"HI")
        self.assertEqual(audio.dtype, np.int16)

    def test_burst_length_all_levels(self):
        for level in range(1, 7):
            mod = Pactor3Modulator(level=level)
            audio = mod.burst(bytes(DATA_BYTES[level]))
            self.assertEqual(len(audio), BURST_SAMPLES, f"level {level}")

    def test_burst_not_silent(self):
        mod = Pactor3Modulator(level=3)
        audio = mod.burst(b"HELLO PACTOR3")
        self.assertGreater(np.max(np.abs(audio.astype(np.int32))), 100)

    def test_level1_round_trip(self):
        data = b"HI"[:DATA_BYTES[1]]
        recovered = self._rt(1, data)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.rstrip(b'\x00'), data.rstrip(b'\x00'))

    def test_level2_round_trip(self):
        data = bytes(range(DATA_BYTES[2]))
        recovered = self._rt(2, data)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered, data)

    def test_level3_round_trip(self):
        data = bytes(range(DATA_BYTES[3]))
        recovered = self._rt(3, data)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered, data)

    def test_level4_round_trip(self):
        data = bytes(range(DATA_BYTES[4]))
        recovered = self._rt(4, data)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered, data)

    def test_level5_round_trip(self):
        rng = np.random.default_rng(15)
        data = rng.integers(0, 256, DATA_BYTES[5], dtype=np.uint8).tobytes()
        recovered = self._rt(5, data)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered, data)

    def test_level6_round_trip(self):
        rng = np.random.default_rng(16)
        data = rng.integers(0, 256, DATA_BYTES[6], dtype=np.uint8).tobytes()
        recovered = self._rt(6, data)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered, data)

    def test_multi_carrier_spectrum(self):
        """Level-3 burst should show energy at all 14 carrier frequencies."""
        mod = Pactor3Modulator(level=3)
        data = bytes(DATA_BYTES[3])
        audio = mod.burst(data).astype(np.float64)
        spectrum = np.abs(np.fft.rfft(audio))
        freqs = np.fft.rfftfreq(len(audio), 1.0 / FS)
        for ci in CARRIER_INDICES[3]:
            fc = CARRIER_FREQS[ci]
            idx = int(round(fc * len(audio) / FS))
            peak = np.max(spectrum[max(0, idx - 3):idx + 4])
            self.assertGreater(peak, 10.0, f"no energy at {fc:.0f} Hz")

    def test_garbage_input_rejected(self):
        demod = Pactor3Demodulator(level=2)
        rng = np.random.default_rng(42)
        noise = rng.integers(-32768, 32768, BURST_SAMPLES, dtype=np.int16)
        self.assertIsNone(demod.feed_burst(noise))


# ---------------------------------------------------------------------------
# Memory ARQ
# ---------------------------------------------------------------------------

class MemArqTest(unittest.TestCase):

    def test_single_decode(self):
        frame = Pactor3Frame(2)
        data = bytes(range(DATA_BYTES[2]))
        carriers = frame.encode(data)
        softs = [np.where(c == 0, 1.0, -1.0) for c in carriers]
        arq = Pactor3MemArq(level=2)
        arq.add(softs)
        result = arq.try_decode()
        self.assertIsNotNone(result)
        self.assertEqual(result['data'], data)

    def test_accumulation_noisy(self):
        """Two noisy transmissions should combine to decode correctly."""
        frame = Pactor3Frame(1)
        data = b"X" * DATA_BYTES[1]
        carriers = frame.encode(data)
        sigma = 0.8
        rng = np.random.default_rng(7)
        arq = Pactor3MemArq(level=1)
        for _ in range(2):
            softs = [
                np.where(c == 0, 1.0, -1.0).astype(np.float64)
                + rng.normal(0, sigma, DATA_SYMS * BPS_PER_LEVEL[1])
                for c in carriers
            ]
            arq.add(softs)
        result = arq.try_decode()
        self.assertIsNotNone(result)
        self.assertEqual(result['data'].rstrip(b'\x00'), data.rstrip(b'\x00'))


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class SessionTest(unittest.TestCase):

    def test_session_level1(self):
        ses = Pactor3Session('W1AW', 'W1XYZ', level=1)
        data = b"HI DE W1AW"[:DATA_BYTES[1]]
        ses.send(data)
        ok = ses.pump_cycle()
        self.assertTrue(ok)
        received = ses.flush()
        self.assertEqual(received.rstrip(b'\x00'), data.rstrip(b'\x00'))

    def test_session_level3(self):
        ses = Pactor3Session('K1TTT', level=3)
        data = bytes(range(DATA_BYTES[3]))
        ses.send(data)
        ok = ses.pump_cycle()
        self.assertTrue(ok)
        self.assertEqual(ses.flush(), data)

    def test_session_level6(self):
        ses = Pactor3Session('VK2XYZ', level=6)
        rng = np.random.default_rng(100)
        data = rng.integers(0, 256, DATA_BYTES[6], dtype=np.uint8).tobytes()
        ses.send(data)
        ok = ses.pump_cycle()
        self.assertTrue(ok)
        self.assertEqual(ses.flush(), data)


# ---------------------------------------------------------------------------
# TNC API
# ---------------------------------------------------------------------------

class TncTest(unittest.TestCase):

    def test_round_trip_level1(self):
        tnc = Pactor3Tnc(level=1)
        data = b"HI WORLD"[:DATA_BYTES[1]]
        audio = tnc.transmit(data)
        recovered = tnc.feed(audio)
        self.assertEqual(recovered, data.rstrip(b'\x00'))

    def test_round_trip_level3(self):
        tnc = Pactor3Tnc(level=3)
        data = bytes(range(DATA_BYTES[3]))
        audio = tnc.transmit(data)
        recovered = tnc.feed(audio)
        self.assertEqual(recovered, data)

    def test_round_trip_level6(self):
        tnc = Pactor3Tnc(level=6)
        rng = np.random.default_rng(66)
        data = rng.integers(0, 256, DATA_BYTES[6], dtype=np.uint8).tobytes()
        audio = tnc.transmit(data)
        recovered = tnc.feed(audio)
        self.assertEqual(recovered, data)

    def test_burst_cycle_timing(self):
        for level in range(1, 7):
            tnc = Pactor3Tnc(level=level)
            audio = tnc.transmit(bytes(DATA_BYTES[level]))
            self.assertEqual(len(audio), BURST_SAMPLES, f"level {level}")

    def test_noise_tolerance_level1(self):
        tnc = Pactor3Tnc(level=1)
        data = b"TEST"[:DATA_BYTES[1]]
        audio = tnc.transmit(data).astype(np.float64)
        rng = np.random.default_rng(99)
        noisy = (audio + rng.normal(0, np.max(np.abs(audio)) * 0.05, len(audio)))
        noisy = np.clip(noisy, -32768, 32767).astype(np.int16)
        recovered = tnc.feed(noisy)
        self.assertEqual(recovered, data.rstrip(b'\x00'))


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
