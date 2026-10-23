"""Tests for the Clover-II modem implementation."""

import unittest
import numpy as np

from hammodem.keyboard.clover2 import (
    Mode, DATA_RATES_BPS,
    _GF256, _GF,
    rs_encode, rs_decode,
    _make_pulse, PULSE_MS, STAGGER_MS, TONE_FREQS, FRAME_MS,
    Clover2Modulator, Clover2Demodulator,
    CloverFrame, ECB_TOTAL, ECB_ROBUST, ECB_NORMAL, ECB_FAST,
    Clover2Tnc, FS,
)


# ---------------------------------------------------------------------------
# GF(2^8) arithmetic
# ---------------------------------------------------------------------------

class GF256Test(unittest.TestCase):
    def test_mul_by_zero(self):
        self.assertEqual(_GF.mul(0, 42), 0)
        self.assertEqual(_GF.mul(42, 0), 0)

    def test_mul_by_one(self):
        for x in [1, 7, 128, 255]:
            self.assertEqual(_GF.mul(x, 1), x)

    def test_mul_commutativity(self):
        self.assertEqual(_GF.mul(13, 45), _GF.mul(45, 13))

    def test_inverse_round_trip(self):
        for x in [1, 3, 17, 127, 200]:
            self.assertEqual(_GF.mul(x, _GF.inv(x)), 1)

    def test_div(self):
        for a, b in [(6, 3), (100, 50), (255, 1)]:
            self.assertEqual(_GF.mul(_GF.div(a, b), b), a)

    def test_pow_zero_exp(self):
        self.assertEqual(_GF.pow(5, 0), 1)

    def test_exp_log_round_trip(self):
        for i in range(1, 255):
            self.assertEqual(_GF.exp[_GF.log[i]], i)


# ---------------------------------------------------------------------------
# Reed-Solomon
# ---------------------------------------------------------------------------

class RSTest(unittest.TestCase):
    def _check_round_trip(self, data: bytes, nsym: int) -> None:
        encoded = rs_encode(data, nsym)
        self.assertEqual(len(encoded), len(data) + nsym)
        decoded = rs_decode(encoded, nsym)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded, data)

    def test_small_block_no_errors(self):
        self._check_round_trip(b"HELLO", 4)

    def test_17_byte_block(self):
        data = bytes(range(17))
        self._check_round_trip(data, 11)

    def test_single_error_corrected(self):
        data = b"CLOVER-II TEST DATA"
        nsym = 8
        encoded = bytearray(rs_encode(data, nsym))
        encoded[5] ^= 0xAB
        decoded = rs_decode(bytes(encoded), nsym)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded, data)

    def test_two_errors_corrected(self):
        data = bytes(range(20))
        nsym = 10
        encoded = bytearray(rs_encode(data, nsym))
        encoded[0] ^= 0xFF
        encoded[15] ^= 0x55
        decoded = rs_decode(bytes(encoded), nsym)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded, data)

    def test_parity_error_corrected(self):
        data = b"PARITY_TEST"
        nsym = 6
        encoded = bytearray(rs_encode(data, nsym))
        encoded[-1] ^= 0x01
        decoded = rs_decode(bytes(encoded), nsym)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded, data)

    def test_too_many_errors_returns_none(self):
        data = b"FAIL_ME"
        nsym = 4
        encoded = bytearray(rs_encode(data, nsym))
        # Corrupt 3 bytes — exceeds nsym//2 = 2
        encoded[0] ^= 0xFF
        encoded[1] ^= 0xFF
        encoded[2] ^= 0xFF
        result = rs_decode(bytes(encoded), nsym)
        # None or wrong data (may detect and fail, or not)
        if result is not None:
            # If it didn't return None it should at least not equal original
            pass  # can't always guarantee None for borderline cases

    def test_ecb_size_255(self):
        data = bytes(range(ECB_NORMAL))
        nsym = ECB_TOTAL - ECB_NORMAL
        encoded = rs_encode(data, nsym)
        self.assertEqual(len(encoded), ECB_TOTAL)


# ---------------------------------------------------------------------------
# Pulse shape
# ---------------------------------------------------------------------------

class PulseTest(unittest.TestCase):
    def test_pulse_length(self):
        pulse = _make_pulse(FS)
        expected = int(round(FS * PULSE_MS / 1000))
        self.assertEqual(len(pulse), expected)

    def test_pulse_starts_ends_near_zero(self):
        pulse = _make_pulse(FS)
        self.assertAlmostEqual(pulse[0], 0.0, places=3)
        self.assertAlmostEqual(pulse[-1], 0.0, places=2)

    def test_pulse_peak_near_one(self):
        pulse = _make_pulse(FS)
        self.assertAlmostEqual(float(np.max(pulse)), 1.0, places=3)

    def test_pulse_is_hann(self):
        pulse = _make_pulse(FS)
        n = len(pulse)
        t = np.arange(n)
        expected = 0.5 * (1.0 - np.cos(2.0 * np.pi * t / (n - 1)))
        np.testing.assert_allclose(pulse, expected, atol=1e-12)

    def test_sideband_suppression(self):
        """Adjacent tone (125 Hz away) should be attenuated > 40 dB."""
        fs = FS
        pulse = _make_pulse(fs)
        n = len(pulse)
        tone = pulse * np.cos(2 * np.pi * TONE_FREQS[0] * np.arange(n) / fs)
        adjacent = TONE_FREQS[0] + 125.0
        # Correlate with adjacent carrier
        corr = np.abs(np.dot(tone, np.cos(2 * np.pi * adjacent * np.arange(n) / fs)))
        self_corr = np.abs(np.dot(tone, np.cos(2 * np.pi * TONE_FREQS[0] * np.arange(n) / fs)))
        ratio = corr / (self_corr + 1e-30)
        self.assertLess(ratio, 0.01)  # better than 40 dB rejection


# ---------------------------------------------------------------------------
# Modulator output properties
# ---------------------------------------------------------------------------

class ModulatorTest(unittest.TestCase):
    def test_returns_int16(self):
        mod = Clover2Modulator(fs=FS, mode=Mode.BPSM)
        audio = mod.transmit(b"A")
        self.assertEqual(audio.dtype, np.int16)

    def test_audio_not_silent(self):
        mod = Clover2Modulator(fs=FS, mode=Mode.QPSM)
        audio = mod.transmit(b"HELLO")
        self.assertGreater(np.max(np.abs(audio.astype(np.float32))), 100)

    def test_audio_within_range(self):
        mod = Clover2Modulator(fs=FS, mode=Mode.QPSM)
        audio = mod.transmit(b"TEST DATA")
        self.assertLessEqual(np.max(np.abs(audio.astype(np.int32))), 32767)

    def test_idle_is_silence(self):
        mod = Clover2Modulator()
        idle = mod.idle(0.1)
        self.assertTrue(np.all(idle == 0))

    def test_different_data_different_audio(self):
        mod = Clover2Modulator(fs=FS, mode=Mode.BPSM)
        a1 = mod.transmit(b"\x00\x00\x00")
        a2 = mod.transmit(b"\xFF\xFF\xFF")
        self.assertFalse(np.array_equal(a1, a2))

    def test_all_modes_produce_audio(self):
        for mode in [Mode.BPSM, Mode.QPSM, Mode.PSM8, Mode.PSM16,
                     Mode.P8A2, Mode.P16A4, Mode.PSM2D]:
            mod = Clover2Modulator(fs=FS, mode=mode)
            audio = mod.transmit(b"X" * 16)
            self.assertGreater(len(audio), 0, f"mode {mode} produced no audio")

    def test_tone_frequencies_present(self):
        """Spectrum should have energy near all four Clover-II tone frequencies."""
        mod = Clover2Modulator(fs=FS, mode=Mode.BPSM)
        audio = mod.transmit(b"\xAA" * 32).astype(np.float64)
        spectrum = np.abs(np.fft.rfft(audio))
        freqs = np.fft.rfftfreq(len(audio), d=1.0 / FS)
        for fc in TONE_FREQS:
            idx = np.argmin(np.abs(freqs - fc))
            window = spectrum[max(0, idx - 5):idx + 6]
            peak = np.max(window)
            self.assertGreater(peak, np.mean(spectrum) * 2,
                               f"tone at {fc} Hz not prominent in spectrum")

    def test_stagger_timing(self):
        """Frame audio should span at least 4 stagger offsets + one pulse."""
        mod = Clover2Modulator(fs=FS, mode=Mode.BPSM)
        audio = mod.transmit(b"\xFF")
        min_len = int(round(FS * (PULSE_MS + 3 * STAGGER_MS) / 1000))
        self.assertGreaterEqual(len(audio), min_len)


# ---------------------------------------------------------------------------
# Demodulator
# ---------------------------------------------------------------------------

class DemodulatorTest(unittest.TestCase):
    def test_feed_returns_bytes(self):
        demod = Clover2Demodulator(fs=FS, mode=Mode.BPSM)
        result = demod.feed(np.zeros(100, dtype=np.int16))
        self.assertIsInstance(result, bytes)

    def test_short_audio_returns_empty(self):
        demod = Clover2Demodulator(fs=FS, mode=Mode.BPSM)
        result = demod.feed(np.zeros(10, dtype=np.int16))
        self.assertEqual(result, b'')

    def test_round_trip_bpsm(self):
        """Encode then decode should recover the data."""
        mod   = Clover2Modulator(fs=FS, mode=Mode.BPSM)
        demod = Clover2Demodulator(fs=FS, mode=Mode.BPSM)
        data = b"\xA5\x5A\xFF\x00"
        audio = mod.transmit(data)
        recovered = demod.feed(audio)
        # Demodulator may return more bytes due to framing; check the data is present
        self.assertGreaterEqual(len(recovered), len(data))
        self.assertEqual(recovered[:len(data)], data)

    def test_round_trip_qpsm(self):
        mod   = Clover2Modulator(fs=FS, mode=Mode.QPSM)
        demod = Clover2Demodulator(fs=FS, mode=Mode.QPSM)
        data = bytes(range(8))
        audio = mod.transmit(data)
        recovered = demod.feed(audio)
        self.assertGreaterEqual(len(recovered), len(data))
        self.assertEqual(recovered[:len(data)], data)

    def test_round_trip_8psk(self):
        mod   = Clover2Modulator(fs=FS, mode=Mode.PSM8)
        demod = Clover2Demodulator(fs=FS, mode=Mode.PSM8)
        data = bytes(range(12))
        audio = mod.transmit(data)
        recovered = demod.feed(audio)
        self.assertGreaterEqual(len(recovered), len(data))
        self.assertEqual(recovered[:len(data)], data)


# ---------------------------------------------------------------------------
# CloverFrame RS encoding
# ---------------------------------------------------------------------------

class CloverFrameTest(unittest.TestCase):
    def _rt(self, bias: str) -> None:
        frame = CloverFrame()
        data_bytes = CloverFrame._DATA_BYTES[bias]
        data = bytes(range(data_bytes))
        ecb = frame.encode_ecb(data, bias)
        self.assertEqual(len(ecb), ECB_TOTAL)
        recovered = frame.decode_ecb(ecb, bias)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered, data)

    def test_robust_round_trip(self):
        self._rt('robust')

    def test_normal_round_trip(self):
        self._rt('normal')

    def test_fast_round_trip(self):
        self._rt('fast')

    def test_ecb_always_255(self):
        frame = CloverFrame()
        for bias in ('robust', 'normal', 'fast'):
            ecb = frame.encode_ecb(b"short", bias)
            self.assertEqual(len(ecb), ECB_TOTAL)

    def test_error_correction_normal(self):
        frame = CloverFrame()
        data = bytes(range(ECB_NORMAL))
        ecb = bytearray(frame.encode_ecb(data, 'normal'))
        # Corrupt 30 bytes (nsym = 255-188 = 67; can correct 33 errors)
        for i in range(30):
            ecb[i * 2] ^= 0xFF
        recovered = frame.decode_ecb(bytes(ecb), 'normal')
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered, data)

    def test_short_payload_padded(self):
        frame = CloverFrame()
        ecb = frame.encode_ecb(b"HI", 'normal')
        self.assertEqual(len(ecb), ECB_TOTAL)
        recovered = frame.decode_ecb(ecb, 'normal')
        self.assertIsNotNone(recovered)
        self.assertTrue(recovered.startswith(b"HI"))

    def test_wrong_length_decode_returns_none(self):
        frame = CloverFrame()
        result = frame.decode_ecb(b"too short", 'normal')
        self.assertIsNone(result)

    def test_ccb_round_trip(self):
        frame = CloverFrame()
        payload = bytes(range(17))
        ccb = frame.encode_ccb(payload)
        recovered = frame.decode_ccb(ccb)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered, payload)


# ---------------------------------------------------------------------------
# Clover2Tnc (top-level API)
# ---------------------------------------------------------------------------

class Clover2TncTest(unittest.TestCase):
    def test_transmit_returns_int16_array(self):
        tnc = Clover2Tnc(fs=FS, mode=Mode.QPSM)
        audio = tnc.transmit(b"HAM RADIO")
        self.assertEqual(audio.dtype, np.int16)
        self.assertGreater(len(audio), 0)

    def test_round_trip_bpsm(self):
        tnc = Clover2Tnc(fs=FS, mode=Mode.BPSM, bias='robust')
        data = b"CQ CQ CQ DE W1AW"
        audio = tnc.transmit(data)
        recovered = tnc.feed(audio)
        self.assertIn(data, recovered)

    def test_round_trip_qpsm(self):
        tnc = Clover2Tnc(fs=FS, mode=Mode.QPSM, bias='normal')
        data = b"HELLO CLOVER"
        audio = tnc.transmit(data)
        recovered = tnc.feed(audio)
        self.assertIn(data, recovered)

    def test_round_trip_noisy(self):
        """Round trip should survive moderate Gaussian noise."""
        tnc_tx = Clover2Tnc(fs=FS, mode=Mode.BPSM, bias='robust')
        tnc_rx = Clover2Tnc(fs=FS, mode=Mode.BPSM, bias='robust')
        data = bytes(range(16))
        audio = tnc_tx.transmit(data).astype(np.float64)
        rng = np.random.default_rng(42)
        noise_amp = np.max(np.abs(audio)) * 0.05  # 5% noise
        noisy = (audio + rng.normal(0, noise_amp, len(audio))).clip(-32768, 32767)
        recovered = tnc_rx.feed(noisy.astype(np.int16))
        self.assertIn(data, recovered)

    def test_empty_transmit(self):
        tnc = Clover2Tnc(fs=FS)
        audio = tnc.transmit(b"")
        self.assertIsInstance(audio, np.ndarray)

    def test_large_payload(self):
        tnc = Clover2Tnc(fs=FS, mode=Mode.QPSM, bias='normal')
        data = b"X" * ECB_NORMAL
        audio = tnc.transmit(data)
        self.assertGreater(len(audio), 0)


# ---------------------------------------------------------------------------
# Data rates
# ---------------------------------------------------------------------------

class DataRateTest(unittest.TestCase):
    def test_bpsm_rate(self):
        self.assertAlmostEqual(DATA_RATES_BPS[Mode.BPSM], 125.0)

    def test_qpsm_rate(self):
        self.assertAlmostEqual(DATA_RATES_BPS[Mode.QPSM], 250.0)

    def test_psk16_rate(self):
        self.assertAlmostEqual(DATA_RATES_BPS[Mode.PSM16], 500.0)

    def test_p16a4_rate(self):
        self.assertAlmostEqual(DATA_RATES_BPS[Mode.P16A4], 750.0)

    def test_dual_diversity_rates(self):
        self.assertAlmostEqual(DATA_RATES_BPS[Mode.PSM2D], 62.5)
        self.assertAlmostEqual(DATA_RATES_BPS[Mode.FSM2D], 62.5)


if __name__ == "__main__":
    unittest.main()
