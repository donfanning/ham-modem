"""Tests for SSTV modem."""

import unittest
import numpy as np

from hammodem.image.sstv import (
    FS, FREQ_SYNC, FREQ_BLACK, FREQ_WHITE,
    VIS_ROBOT36, VIS_MARTIN_M1, VIS_MARTIN_M2, VIS_SCOTTIE_S1, VIS_SCOTTIE_S2,
    SstvEncoder, SstvDecoder, Sstv,
    _vis_header, _decode_vis, _rgb_to_ycrcb, _ycrcb_to_rgb,
    _fm_demodulate, _freqs_to_pixels, _resize_image,
)


class ConstantsTest(unittest.TestCase):

    def test_vis_codes(self):
        self.assertEqual(VIS_ROBOT36,    8)
        self.assertEqual(VIS_MARTIN_M2, 40)
        self.assertEqual(VIS_MARTIN_M1, 44)
        self.assertEqual(VIS_SCOTTIE_S2, 56)
        self.assertEqual(VIS_SCOTTIE_S1, 60)

    def test_freq_range(self):
        self.assertAlmostEqual(FREQ_WHITE - FREQ_BLACK, 800.0)

    def test_sync_freq(self):
        self.assertAlmostEqual(FREQ_SYNC, 1200.0)


class VisHeaderTest(unittest.TestCase):

    def _decode(self, vis_code):
        audio, _ = _vis_header(vis_code, FS)
        audio_i16 = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
        freq = _fm_demodulate(audio_i16, fc=1900.0, fs=FS)
        return _decode_vis(freq, FS)

    def test_robot36_vis(self):
        self.assertEqual(self._decode(VIS_ROBOT36), VIS_ROBOT36)

    def test_martin_m1_vis(self):
        self.assertEqual(self._decode(VIS_MARTIN_M1), VIS_MARTIN_M1)

    def test_martin_m2_vis(self):
        self.assertEqual(self._decode(VIS_MARTIN_M2), VIS_MARTIN_M2)

    def test_scottie_s1_vis(self):
        self.assertEqual(self._decode(VIS_SCOTTIE_S1), VIS_SCOTTIE_S1)

    def test_scottie_s2_vis(self):
        self.assertEqual(self._decode(VIS_SCOTTIE_S2), VIS_SCOTTIE_S2)

    def test_vis_header_not_silent(self):
        audio, _ = _vis_header(VIS_ROBOT36, FS)
        self.assertGreater(np.max(np.abs(audio)), 0.1)

    def test_decode_vis_noise_returns_none_or_int(self):
        rng = np.random.default_rng(0)
        noise = rng.integers(-32768, 32768, 8000, dtype=np.int16)
        freq = _fm_demodulate(noise)
        result = _decode_vis(freq, FS)
        # Must be None or an integer — must not raise
        self.assertTrue(result is None or isinstance(result, int))


class YCrCbTest(unittest.TestCase):

    def test_round_trip_gray(self):
        rgb = np.stack([
            np.full((10, 10), 128, dtype=np.uint8),
            np.full((10, 10), 128, dtype=np.uint8),
            np.full((10, 10), 128, dtype=np.uint8),
        ], axis=2)
        ycrcb = _rgb_to_ycrcb(rgb)
        recovered = _ycrcb_to_rgb(ycrcb)
        mae = np.mean(np.abs(rgb.astype(int) - recovered.astype(int)))
        self.assertLess(mae, 5.0)

    def test_round_trip_random(self):
        rng = np.random.default_rng(11)
        rgb = rng.integers(0, 256, (8, 8, 3), dtype=np.uint8)
        ycrcb = _rgb_to_ycrcb(rgb)
        recovered = _ycrcb_to_rgb(ycrcb)
        mae = np.mean(np.abs(rgb.astype(int) - recovered.astype(int)))
        self.assertLess(mae, 8.0)

    def test_ycrcb_shape(self):
        rgb = np.zeros((4, 4, 3), dtype=np.uint8)
        ycrcb = _rgb_to_ycrcb(rgb)
        self.assertEqual(ycrcb.shape, (4, 4, 3))


class FreqsToPixelsTest(unittest.TestCase):

    def test_black_maps_to_zero(self):
        freq = np.full(100, FREQ_BLACK)
        px = _freqs_to_pixels(freq, 20)
        self.assertTrue(np.all(px == 0))

    def test_white_maps_to_255(self):
        freq = np.full(100, FREQ_WHITE)
        px = _freqs_to_pixels(freq, 20)
        self.assertTrue(np.all(px == 255))

    def test_output_length(self):
        freq = np.full(200, 1900.0)
        px = _freqs_to_pixels(freq, 50)
        self.assertEqual(len(px), 50)


class ResizeTest(unittest.TestCase):

    def test_resize_up(self):
        img = np.zeros((10, 10, 3), dtype=np.uint8)
        out = _resize_image(img, 20, 40)
        self.assertEqual(out.shape, (20, 40, 3))

    def test_resize_down(self):
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        out = _resize_image(img, 50, 50)
        self.assertEqual(out.shape, (50, 50, 3))


class EncoderApiTest(unittest.TestCase):

    def test_known_modes(self):
        for mode in Sstv.MODES:
            enc = SstvEncoder(mode)
            self.assertGreater(enc.width, 0)
            self.assertGreater(enc.height, 0)

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            SstvEncoder('alien_mode')

    def test_wrong_ndim_raises(self):
        enc = SstvEncoder('robot36')
        with self.assertRaises(ValueError):
            enc.encode(np.zeros((240, 320), dtype=np.uint8))

    def test_wrong_channels_raises(self):
        enc = SstvEncoder('robot36')
        with self.assertRaises(ValueError):
            enc.encode(np.zeros((240, 320, 4), dtype=np.uint8))

    def test_output_dtype_int16(self):
        enc = SstvEncoder('robot36')
        img = np.zeros((enc.height, enc.width, 3), dtype=np.uint8)
        audio = enc.encode(img)
        self.assertEqual(audio.dtype, np.int16)

    def test_output_not_silent(self):
        enc = SstvEncoder('robot36')
        rng = np.random.default_rng(1)
        img = rng.integers(0, 256, (enc.height, enc.width, 3), dtype=np.uint8)
        audio = enc.encode(img)
        self.assertGreater(np.max(np.abs(audio.astype(np.int32))), 100)


class Robot36Test(unittest.TestCase):

    def _make_image(self, seed=0):
        enc = SstvEncoder('robot36')
        rng = np.random.default_rng(seed)
        return rng.integers(20, 236, (enc.height, enc.width, 3), dtype=np.uint8)

    def test_encode_length_positive(self):
        enc = SstvEncoder('robot36')
        img = self._make_image()
        audio = enc.encode(img)
        self.assertGreater(len(audio), 0)

    def test_round_trip_shape(self):
        sstv = Sstv('robot36')
        img = self._make_image(1)
        audio = sstv.encode(img)
        recovered = sstv.decode(audio)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.shape[2], 3)

    def test_round_trip_pixel_fidelity(self):
        """Smooth gradient image should survive Robot36 YCrCb round-trip."""
        sstv = Sstv('robot36')
        enc = SstvEncoder('robot36')
        # Horizontal gradient: low spatial frequency → small FM demod error
        grad = np.linspace(40, 215, enc.width, dtype=np.float32).astype(np.uint8)
        img = np.stack([
            np.tile(grad, (enc.height, 1)),
            np.tile(grad, (enc.height, 1)),
            np.tile(grad, (enc.height, 1)),
        ], axis=2)
        audio = sstv.encode(img)
        recovered = sstv.decode(audio)
        self.assertIsNotNone(recovered)
        h = min(img.shape[0], recovered.shape[0])
        mae = np.mean(np.abs(img[:h].astype(int) - recovered[:h].astype(int)))
        self.assertLess(mae, 20.0, f"Robot36 MAE {mae:.1f}")

    def test_all_black(self):
        sstv = Sstv('robot36')
        enc = SstvEncoder('robot36')
        img = np.zeros((enc.height, enc.width, 3), dtype=np.uint8)
        audio = sstv.encode(img)
        recovered = sstv.decode(audio)
        self.assertIsNotNone(recovered)

    def test_vis_code_detected(self):
        enc = SstvEncoder('robot36')
        img = np.zeros((enc.height, enc.width, 3), dtype=np.uint8)
        audio = enc.encode(img)
        freq = _fm_demodulate(audio)
        vis = _decode_vis(freq, FS)
        self.assertEqual(vis, VIS_ROBOT36)


class MartinM1Test(unittest.TestCase):

    def _make_image(self, seed=10):
        enc = SstvEncoder('martin_m1')
        rng = np.random.default_rng(seed)
        return rng.integers(20, 236, (enc.height, enc.width, 3), dtype=np.uint8)

    def test_encode_not_silent(self):
        enc = SstvEncoder('martin_m1')
        img = self._make_image()
        audio = enc.encode(img)
        self.assertGreater(np.max(np.abs(audio.astype(np.int32))), 100)

    def test_vis_code_detected(self):
        enc = SstvEncoder('martin_m1')
        img = self._make_image()
        audio = enc.encode(img)
        freq = _fm_demodulate(audio)
        vis = _decode_vis(freq, FS)
        self.assertEqual(vis, VIS_MARTIN_M1)

    def test_round_trip_shape(self):
        sstv = Sstv('martin_m1')
        img = self._make_image(11)
        audio = sstv.encode(img)
        recovered = sstv.decode(audio)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.ndim, 3)
        self.assertEqual(recovered.shape[2], 3)


class MartinM2Test(unittest.TestCase):

    def test_encode_shorter_than_m1(self):
        enc1 = SstvEncoder('martin_m1')
        enc2 = SstvEncoder('martin_m2')
        rng = np.random.default_rng(20)
        img1 = rng.integers(0, 256, (enc1.height, enc1.width, 3), dtype=np.uint8)
        img2 = rng.integers(0, 256, (enc2.height, enc2.width, 3), dtype=np.uint8)
        a1 = enc1.encode(img1)
        a2 = enc2.encode(img2)
        self.assertLess(len(a2), len(a1))

    def test_vis_code_detected(self):
        enc = SstvEncoder('martin_m2')
        rng = np.random.default_rng(21)
        img = rng.integers(0, 256, (enc.height, enc.width, 3), dtype=np.uint8)
        audio = enc.encode(img)
        freq = _fm_demodulate(audio)
        vis = _decode_vis(freq, FS)
        self.assertEqual(vis, VIS_MARTIN_M2)

    def test_round_trip_shape(self):
        sstv = Sstv('martin_m2')
        enc = SstvEncoder('martin_m2')
        rng = np.random.default_rng(22)
        img = rng.integers(20, 236, (enc.height, enc.width, 3), dtype=np.uint8)
        audio = sstv.encode(img)
        recovered = sstv.decode(audio)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.shape[2], 3)


class ScottieS1Test(unittest.TestCase):

    def test_vis_code_detected(self):
        enc = SstvEncoder('scottie_s1')
        rng = np.random.default_rng(30)
        img = rng.integers(0, 256, (enc.height, enc.width, 3), dtype=np.uint8)
        audio = enc.encode(img)
        freq = _fm_demodulate(audio)
        vis = _decode_vis(freq, FS)
        self.assertEqual(vis, VIS_SCOTTIE_S1)

    def test_encode_produces_audio(self):
        enc = SstvEncoder('scottie_s1')
        rng = np.random.default_rng(31)
        img = rng.integers(0, 256, (enc.height, enc.width, 3), dtype=np.uint8)
        audio = enc.encode(img)
        self.assertEqual(audio.dtype, np.int16)
        self.assertGreater(len(audio), 0)

    def test_round_trip_shape(self):
        sstv = Sstv('scottie_s1')
        enc = SstvEncoder('scottie_s1')
        rng = np.random.default_rng(32)
        img = rng.integers(20, 236, (enc.height, enc.width, 3), dtype=np.uint8)
        audio = sstv.encode(img)
        recovered = sstv.decode(audio)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.shape[2], 3)


class ScottieS2Test(unittest.TestCase):

    def test_encode_shorter_than_s1(self):
        enc1 = SstvEncoder('scottie_s1')
        enc2 = SstvEncoder('scottie_s2')
        rng = np.random.default_rng(40)
        img = rng.integers(0, 256, (enc1.height, enc1.width, 3), dtype=np.uint8)
        a1 = enc1.encode(img)
        a2 = enc2.encode(img)
        self.assertLess(len(a2), len(a1))

    def test_vis_code_detected(self):
        enc = SstvEncoder('scottie_s2')
        rng = np.random.default_rng(41)
        img = rng.integers(0, 256, (enc.height, enc.width, 3), dtype=np.uint8)
        audio = enc.encode(img)
        freq = _fm_demodulate(audio)
        vis = _decode_vis(freq, FS)
        self.assertEqual(vis, VIS_SCOTTIE_S2)

    def test_round_trip_shape(self):
        sstv = Sstv('scottie_s2')
        enc = SstvEncoder('scottie_s2')
        rng = np.random.default_rng(42)
        img = rng.integers(20, 236, (enc.height, enc.width, 3), dtype=np.uint8)
        audio = sstv.encode(img)
        recovered = sstv.decode(audio)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.shape[2], 3)


class DecoderAutoModeTest(unittest.TestCase):
    """Decoder must auto-detect mode from VIS header."""

    def test_auto_detects_robot36(self):
        sstv = Sstv('robot36')
        enc = SstvEncoder('robot36')
        rng = np.random.default_rng(50)
        img = rng.integers(0, 256, (enc.height, enc.width, 3), dtype=np.uint8)
        audio = sstv.encode(img)
        dec = SstvDecoder()
        recovered = dec.decode(audio)
        self.assertIsNotNone(recovered)

    def test_noise_no_crash(self):
        dec = SstvDecoder()
        rng = np.random.default_rng(99)
        noise = rng.integers(-32768, 32768, 16000, dtype=np.int16)
        result = dec.decode(noise)
        self.assertIsNone(result)

    def test_wrong_vis_returns_none(self):
        """Encoding one mode but decoding with an unknown VIS returns None."""
        dec = SstvDecoder()
        # Generate a pure-tone signal (not a valid SSTV transmission)
        t = np.arange(16000) / FS
        pure = (np.sin(2 * np.pi * 1700.0 * t) * 32767).astype(np.int16)
        result = dec.decode(pure)
        self.assertIsNone(result)


class SstvWrapperTest(unittest.TestCase):

    def test_width_height(self):
        for mode in Sstv.MODES:
            s = Sstv(mode)
            self.assertGreater(s.width, 0)
            self.assertGreater(s.height, 0)

    def test_encode_returns_int16(self):
        for mode in Sstv.MODES:
            s = Sstv(mode)
            rng = np.random.default_rng(0)
            img = rng.integers(0, 256, (s.height, s.width, 3), dtype=np.uint8)
            audio = s.encode(img)
            self.assertEqual(audio.dtype, np.int16, f"mode={mode}")


if __name__ == '__main__':
    unittest.main()
