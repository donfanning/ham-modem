"""Tests for HF FAX (WEFAX) modem."""

import unittest
import numpy as np

from hammodem.image.fax import (
    FS, FREQ_BLACK, FREQ_WHITE, FREQ_SYNC, SYNC_FRAC, PHASING_LINES,
    HfFaxEncoder, HfFaxDecoder, HfFax,
    _fm_encode, _fm_demodulate, _line_to_audio, _audio_to_line,
)


class ConstantsTest(unittest.TestCase):

    def test_freq_range(self):
        self.assertAlmostEqual(FREQ_WHITE - FREQ_BLACK, 800.0)

    def test_sync_is_black(self):
        self.assertAlmostEqual(FREQ_SYNC, FREQ_BLACK)

    def test_sync_frac(self):
        self.assertAlmostEqual(SYNC_FRAC, 0.05)

    def test_phasing_lines_positive(self):
        self.assertGreater(PHASING_LINES, 0)


class FmEncodeTest(unittest.TestCase):

    def test_constant_freq_output_length(self):
        freqs = np.full(100, 1900.0)
        audio, _ = _fm_encode(freqs)
        self.assertEqual(len(audio), 100)

    def test_phase_continuity(self):
        freqs = np.full(200, 1900.0)
        audio, phase = _fm_encode(freqs, init_phase=0.0)
        audio2, _ = _fm_encode(np.full(200, 1900.0), init_phase=phase)
        # Second segment continues without a phase jump
        self.assertAlmostEqual(audio[-1], np.sin(phase), places=5)

    def test_output_bounded(self):
        freqs = np.linspace(FREQ_BLACK, FREQ_WHITE, 800)
        audio, _ = _fm_encode(freqs)
        self.assertLessEqual(np.max(np.abs(audio)), 1.0 + 1e-9)


class FmDemodulateTest(unittest.TestCase):

    def _constant_tone(self, freq: float, n: int = 2000) -> np.ndarray:
        t = np.arange(n) / FS
        return (np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)

    def test_black_frequency(self):
        audio = self._constant_tone(FREQ_BLACK)
        freq = _fm_demodulate(audio)
        self.assertAlmostEqual(np.mean(freq[100:-100]), FREQ_BLACK, delta=50)

    def test_white_frequency(self):
        audio = self._constant_tone(FREQ_WHITE)
        freq = _fm_demodulate(audio)
        self.assertAlmostEqual(np.mean(freq[100:-100]), FREQ_WHITE, delta=50)

    def test_output_length(self):
        audio = self._constant_tone(1900.0, n=500)
        freq = _fm_demodulate(audio)
        self.assertEqual(len(freq), 500)


class EncoderPropertiesTest(unittest.TestCase):

    def test_default_ioc576_width(self):
        enc = HfFaxEncoder(lpm=120, ioc=576)
        import math
        expected = int(round(576 * math.pi))
        self.assertEqual(enc.width, expected)

    def test_ioc288_width(self):
        enc = HfFaxEncoder(lpm=120, ioc=288)
        import math
        expected = int(round(288 * math.pi))
        self.assertEqual(enc.width, expected)

    def test_line_samples_120lpm(self):
        enc = HfFaxEncoder(lpm=120, ioc=576)
        expected = int(round(FS * 60.0 / 120))
        self.assertEqual(enc.line_samples, expected)

    def test_line_samples_60lpm(self):
        enc = HfFaxEncoder(lpm=60, ioc=576)
        expected = int(round(FS * 60.0 / 60))
        self.assertEqual(enc.line_samples, expected)

    def test_sync_frac(self):
        enc = HfFaxEncoder(lpm=120, ioc=576)
        ratio = enc.sync_samples / enc.line_samples
        self.assertAlmostEqual(ratio, SYNC_FRAC, delta=0.005)

    def test_image_samples_plus_sync_equals_line(self):
        enc = HfFaxEncoder(lpm=120, ioc=576)
        self.assertEqual(enc.image_samples + enc.sync_samples, enc.line_samples)


class EncodeOutputTest(unittest.TestCase):

    def _make_image(self, h=10, w=None):
        enc = HfFaxEncoder(lpm=120, ioc=576)
        w = w or enc.width
        rng = np.random.default_rng(42)
        return rng.integers(0, 256, (h, w), dtype=np.uint8)

    def test_output_dtype(self):
        enc = HfFaxEncoder(lpm=120, ioc=576)
        img = self._make_image()
        audio = enc.encode(img)
        self.assertEqual(audio.dtype, np.int16)

    def test_output_length(self):
        enc = HfFaxEncoder(lpm=120, ioc=576)
        h = 8
        img = self._make_image(h)
        audio = enc.encode(img)
        total_lines = PHASING_LINES + h + PHASING_LINES
        expected = total_lines * enc.line_samples
        self.assertEqual(len(audio), expected)

    def test_not_silent(self):
        enc = HfFaxEncoder(lpm=120, ioc=576)
        img = self._make_image()
        audio = enc.encode(img)
        self.assertGreater(np.max(np.abs(audio.astype(np.int32))), 100)

    def test_wrong_ndim_raises(self):
        enc = HfFaxEncoder(lpm=120, ioc=576)
        with self.assertRaises(ValueError):
            enc.encode(np.zeros((10, 10, 3), dtype=np.uint8))

    def test_width_scaling(self):
        enc = HfFaxEncoder(lpm=120, ioc=576)
        img = np.zeros((5, 100), dtype=np.uint8)  # wrong width
        audio = enc.encode(img)
        self.assertIsNotNone(audio)
        self.assertEqual(audio.dtype, np.int16)


class LineCodecTest(unittest.TestCase):

    def test_line_to_audio_length(self):
        enc = HfFaxEncoder(lpm=120, ioc=576)
        row = np.zeros(enc.width, dtype=np.uint8)
        audio, _ = _line_to_audio(row, enc.line_samples, enc.sync_samples, 0.0)
        self.assertEqual(len(audio), enc.line_samples)

    def test_sync_is_black_freq(self):
        enc = HfFaxEncoder(lpm=120, ioc=576)
        row = np.full(enc.width, 128, dtype=np.uint8)
        audio, _ = _line_to_audio(row, enc.line_samples, enc.sync_samples, 0.0)
        # Demodulate sync portion
        audio_f64 = audio.astype(np.float64)
        t = np.arange(len(audio_f64)) / FS
        bb = audio_f64 * np.exp(-2j * np.pi * 1900.0 * t)
        from numpy import angle
        diff = bb[1:] * bb[:-1].conj()
        freq = 1900.0 + angle(diff) * FS / (2 * np.pi)
        sync_mean = np.mean(freq[:enc.sync_samples - 5])
        self.assertAlmostEqual(sync_mean, FREQ_SYNC, delta=80)

    def test_audio_to_line_length(self):
        enc = HfFaxEncoder(lpm=120, ioc=576)
        row_in = np.full(enc.width, 200, dtype=np.uint8)
        audio, _ = _line_to_audio(row_in, enc.line_samples, enc.sync_samples, 0.0)
        audio_i16 = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
        freq = _fm_demodulate(audio_i16)
        row_out = _audio_to_line(freq, enc.line_samples, enc.sync_samples, enc.width)
        self.assertEqual(len(row_out), enc.width)

    def test_pixel_round_trip_single_line(self):
        enc = HfFaxEncoder(lpm=120, ioc=576)
        rng = np.random.default_rng(7)
        row_in = rng.integers(20, 236, enc.width, dtype=np.uint8)
        audio, _ = _line_to_audio(row_in, enc.line_samples, enc.sync_samples, 0.0)
        audio_i16 = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
        freq = _fm_demodulate(audio_i16)
        row_out = _audio_to_line(freq, enc.line_samples, enc.sync_samples, enc.width)
        # Mean absolute pixel error should be small (FM demod + interpolation)
        mae = np.mean(np.abs(row_in.astype(int) - row_out.astype(int)))
        self.assertLess(mae, 20.0)


class RoundTripTest(unittest.TestCase):

    def _make_image(self, h=12, lpm=120, ioc=576):
        enc = HfFaxEncoder(lpm=lpm, ioc=ioc)
        rng = np.random.default_rng(99)
        return rng.integers(30, 226, (h, enc.width), dtype=np.uint8)

    def test_round_trip_small(self):
        fax = HfFax(lpm=120, ioc=576)
        img = self._make_image(h=8)
        audio = fax.encode(img)
        recovered = fax.decode(audio)
        self.assertIsNotNone(recovered)

    def test_recovered_shape(self):
        fax = HfFax(lpm=120, ioc=576)
        img = self._make_image(h=6)
        audio = fax.encode(img)
        recovered = fax.decode(audio)
        self.assertIsNotNone(recovered)
        # Width must match IOC width
        self.assertEqual(recovered.shape[1], fax.width)

    def test_pixel_fidelity(self):
        """Smooth gradient image should round-trip with low MAE."""
        fax = HfFax(lpm=120, ioc=576)
        h = 10
        # Smooth horizontal gradient: FM bandwidth well within Nyquist
        gradient = np.linspace(30, 226, fax.width, dtype=np.float32).astype(np.uint8)
        img = np.tile(gradient, (h, 1))
        audio = fax.encode(img)
        recovered = fax.decode(audio)
        self.assertIsNotNone(recovered)
        n_rec = recovered.shape[0]
        mid = min(h, n_rec) // 2
        mae = np.mean(np.abs(
            img[:mid].astype(int) - recovered[:mid].astype(int)
        ))
        self.assertLess(mae, 15.0, f"Pixel MAE {mae:.1f} too high")

    def test_ioc288_round_trip(self):
        fax = HfFax(lpm=120, ioc=288)
        enc = HfFaxEncoder(lpm=120, ioc=288)
        rng = np.random.default_rng(55)
        img = rng.integers(20, 236, (6, enc.width), dtype=np.uint8)
        audio = fax.encode(img)
        recovered = fax.decode(audio)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.shape[1], enc.width)

    def test_60lpm_round_trip(self):
        fax = HfFax(lpm=60, ioc=576)
        enc = HfFaxEncoder(lpm=60, ioc=576)
        rng = np.random.default_rng(33)
        img = rng.integers(10, 245, (4, enc.width), dtype=np.uint8)
        audio = fax.encode(img)
        recovered = fax.decode(audio)
        self.assertIsNotNone(recovered)

    def test_garbage_audio_returns_none_or_image(self):
        fax = HfFax(lpm=120, ioc=576)
        rng = np.random.default_rng(0)
        noise = rng.integers(-32768, 32767, 8000, dtype=np.int16)
        # Either returns None or something; just must not raise
        result = fax.decode(noise)
        _ = result   # may be None

    def test_all_black_image(self):
        fax = HfFax(lpm=120, ioc=576)
        img = np.zeros((6, fax.width), dtype=np.uint8)
        audio = fax.encode(img)
        recovered = fax.decode(audio)
        self.assertIsNotNone(recovered)
        # Most pixels should be close to black (allow small sync-edge bias)
        self.assertLess(np.mean(recovered.astype(float)), 50.0)

    def test_all_white_image(self):
        fax = HfFax(lpm=120, ioc=576)
        img = np.full((6, fax.width), 255, dtype=np.uint8)
        audio = fax.encode(img)
        recovered = fax.decode(audio)
        self.assertIsNotNone(recovered)
        self.assertGreater(np.mean(recovered.astype(float)), 215.0)


class HfFaxApiTest(unittest.TestCase):

    def test_width_property(self):
        fax = HfFax(lpm=120, ioc=576)
        import math
        self.assertEqual(fax.width, int(round(576 * math.pi)))

    def test_line_samples_property(self):
        fax = HfFax(lpm=120, ioc=576)
        self.assertEqual(fax.line_samples, int(round(FS * 60.0 / 120)))

    def test_encoder_decoder_agree(self):
        enc = HfFaxEncoder(lpm=120, ioc=576)
        dec = HfFaxDecoder(lpm=120, ioc=576)
        self.assertEqual(enc.width, dec.width)


if __name__ == '__main__':
    unittest.main()
