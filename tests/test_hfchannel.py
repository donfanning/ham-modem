import math
import unittest

import numpy as np

from hammodem.hfchannel import PRESETS, HfChannel, _Doppler


def run(ch, x, block=160):
    return np.concatenate([ch.process(x[i:i + block]) for i in range(0, len(x), block)])


class HfChannelTest(unittest.TestCase):
    def test_transparent_without_fading_or_noise(self):
        ch = HfChannel(8000, "awgn", snr_db=None, seed=1)
        x = np.random.default_rng(2).normal(0, 3000, 8000)
        y = run(ch, x, block=137)
        gd = ch.gd
        np.testing.assert_allclose(y[gd:], x[:-gd], atol=1e-9)        # a pure delay by the Hilbert group delay

    def test_doppler_unit_power_and_spread(self):
        fs = 8000
        for spread in (0.5, 1.0, 10.0):
            d = _Doppler(spread, fs, np.random.default_rng(3))
            g = np.concatenate([d.take(4000) for _ in range(int(400 / spread) + 40)])
            self.assertAlmostEqual(float(np.mean(np.abs(g) ** 2)), 1.0, delta=0.25)
            # two-sigma spread from the spectrum of g (decimated to well above the spread)
            dec = max(1, int(fs / (40 * spread)))
            gd = g[::dec]
            spec = np.abs(np.fft.fftshift(np.fft.fft(gd))) ** 2
            f = np.fft.fftshift(np.fft.fftfreq(len(gd), dec / fs))
            sigma = math.sqrt(float(np.sum(spec * f ** 2) / np.sum(spec)))
            self.assertAlmostEqual(2 * sigma, spread, delta=0.25 * spread)

    def test_rayleigh_envelope(self):
        d = _Doppler(1.0, 8000, np.random.default_rng(4))
        a = np.abs(np.concatenate([d.take(8000) for _ in range(600)]))
        # Rayleigh with E|g|^2 = 1: P(|g| < r) = 1 - exp(-r^2); check the median (sqrt(ln 2)).
        self.assertAlmostEqual(float(np.median(a)), math.sqrt(math.log(2)), delta=0.1)

    def test_two_path_delay(self):
        fs = 8000
        ch = HfChannel(fs, "poor", snr_db=None, seed=5)
        ch.g1.take = ch.g2.take = lambda n: np.ones(n, complex)      # freeze fading: exact taps
        x = np.zeros(4000)
        x[1000] = 10000.0
        y = run(ch, x)
        env = np.abs(y)
        peaks = sorted(np.argsort(env)[-2:])
        self.assertEqual(peaks[1] - peaks[0], int(2.0 * fs / 1000))   # the second path 2 ms later

    def test_snr_calibration_in_3khz(self):
        fs, snr = 8000, 10.0
        ch = HfChannel(fs, "awgn", snr_db=snr, signal_dbfs=-20.0, seed=6)
        y = run(ch, np.zeros(fs * 20))
        ps = 10 ** (-20 / 10) * 32767 ** 2
        spec = np.abs(np.fft.rfft(y)) ** 2
        f = np.fft.rfftfreq(len(y), 1 / fs)
        band = (f > 300) & (f <= 3300)
        noise_3k = float(np.sum(spec[band]) / np.sum(spec)) * float(np.mean(y * y))
        self.assertAlmostEqual(10 * math.log10(ps / noise_3k), snr, delta=0.5)

    def test_presets_exist(self):
        for name in ("good", "moderate", "poor", "flutter"):
            self.assertIn(name, PRESETS)


if __name__ == "__main__":
    unittest.main()
