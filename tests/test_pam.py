import unittest

import numpy as np

from hammodem.modems import make
from hammodem.modems.pam import GRAY, PamDemodulator, PamModulator
from tests import impair
from tests.test_afsk import make_frames


def roundtrip(name, fs, fn, n=12, block=None):
    frames = make_frames(n, extra=200)
    mod, dem = make(name, fs)
    audio = np.concatenate([np.concatenate((mod.modulate([f]), np.zeros(fs // 20, np.int16))) for f in frames]
                           ).astype(float)
    pcm = impair.to_pcm(fn(audio, np.random.default_rng(2)))
    got = []
    block = block or fs // 100
    for i in range(0, len(pcm), block):
        got += dem.feed(pcm[i:i + block])
    return frames, got


class PamTest(unittest.TestCase):
    def check(self, name, fs, fn, minimum, **kw):
        frames, got = roundtrip(name, fs, fn, **kw)
        self.assertEqual(len(got), sum(g in frames for g in got), "only transmitted frames may come out")
        self.assertGreaterEqual(sum(g in frames for g in got), minimum)

    def test_gray_mapping_one_bit_between_neighbours(self):
        order = sorted(GRAY, key=GRAY.get)
        for a, b in zip(order, order[1:]):
            self.assertEqual(sum(x != y for x, y in zip(a, b)), 1)

    def test_57600_clean_gain_and_drift(self):
        self.check("pam57600", 48000, lambda a, r: a, 12)
        self.check("pam57600", 48000, lambda a, r: a * 0.4, 12)
        self.check("pam57600", 48000, lambda a, r: impair.noise(impair.drift_bandlimited(a, -300), 500, r), 12)

    def test_57600_noise(self):
        self.check("pam57600", 48000, lambda a, r: impair.noise(a, 800, r), 12)

    def test_19200_on_16k(self):
        self.check("pam19200", 16000, lambda a, r: impair.noise(impair.drift_bandlimited(a, 200), 500, r), 12)

    def test_block_size_independent(self):
        for block in (7, 480, 4000):
            frames, got = roundtrip("pam57600", 48000, lambda a, r: a, n=3, block=block)
            self.assertEqual(got, frames)

    def test_refuses_narrow_legs(self):
        with self.assertRaises(ValueError):
            PamModulator(16000, 28800)
        with self.assertRaises(ValueError):
            PamDemodulator(8000, 9600)

    def test_dcd(self):
        dem = PamDemodulator(48000, 28800, energy_dcd_dbfs=None)
        dem.feed(np.zeros(4800, np.int16))
        self.assertFalse(dem.dcd)
        dem.feed(PamModulator(48000, 28800).modulate_bits([0, 1, 1, 1, 1, 1, 1, 0] * 400))
        self.assertTrue(dem.dcd)
        dem.feed(np.zeros(9600, np.int16))
        self.assertFalse(dem.dcd)


if __name__ == "__main__":
    unittest.main()
