import time
import unittest

import numpy as np

from hammodem.ax25 import Frame
from hammodem.modems.afsk import BELL202, HF300, AfskDemodulator, AfskModulator
from tests import impair


def make_frames(n, extra=40):
    return [Frame.ui("N0CALL-1", "APRS", f"!4903.50N/07201.75W-Test {i} " + "x" * extra, "WIDE1-1").encode()
            for i in range(n)]


def transmit(profile, frames, fs=8000, gap=800):
    mod = AfskModulator(profile, fs)
    return np.concatenate([np.concatenate((mod.modulate([f]), np.zeros(gap, np.int16))) for f in frames]).astype(float)


def receive(profile, audio, fs=8000, block=160):
    dem, got = AfskDemodulator(profile, fs), []
    pcm = impair.to_pcm(audio)
    for i in range(0, len(pcm), block):
        got += dem.feed(pcm[i:i + block])
    return got, dem


class AfskTest(unittest.TestCase):
    def check(self, profile, fn, minimum, n=20, fs=8000):
        frames = make_frames(n)
        got, _ = receive(profile, fn(transmit(profile, frames, fs), np.random.default_rng(11)), fs)
        good = sum(g in frames for g in got)
        self.assertEqual(len(got), good, "every reported frame must be a transmitted one")
        self.assertEqual(len(set(got)), len(got), "no duplicates from the slicer bank")
        self.assertGreaterEqual(good, minimum)

    def test_1200_clean(self):
        self.check(BELL202, lambda a, r: a, 20)

    def test_1200_noise(self):
        self.check(BELL202, lambda a, r: impair.noise(a, 4000, r), 20)

    def test_1200_twist(self):
        self.check(BELL202, lambda a, r: impair.noise(impair.deemphasis(a), 3000, r), 20)
        self.check(BELL202, lambda a, r: impair.noise(impair.preemphasis(a), 3000, r), 20)

    def test_1200_clock_drift(self):
        for ppm in (-500, 500):
            self.check(BELL202, lambda a, r: impair.noise(impair.drift(a, ppm), 3000, r), 20)

    def test_1200_wideband_rate(self):
        self.check(BELL202, lambda a, r: impair.noise(a, 3000, r), 20, fs=16000)

    def test_300_noise_and_drift(self):
        self.check(HF300, lambda a, r: impair.noise(impair.drift(a, 300), 9000, r), 10, n=10)

    def test_back_to_back_frames_in_one_burst(self):
        frames = make_frames(5)
        audio = AfskModulator(BELL202).modulate(frames).astype(float)
        got, _ = receive(BELL202, audio)
        self.assertEqual(got, frames)

    def test_block_size_independent(self):
        frames = make_frames(3)
        audio = transmit(BELL202, frames)
        for block in (1, 37, 160, 4000):
            self.assertEqual(receive(BELL202, audio, block=block)[0], frames)

    def test_modulator_continuity(self):
        mod = AfskModulator(BELL202)
        a = np.concatenate([mod.modulate_bits([1] * 7) for _ in range(30)]).astype(float)
        self.assertLess(np.max(np.abs(np.diff(a))), 0.5 * 32767 * 2 * np.pi * 2200 / 8000 * 1.01)
        self.assertEqual(len(a), round(30 * 7 * 8000 / 1200))

    def test_dcd_quality(self):
        dem = AfskDemodulator(BELL202, energy_dcd_dbfs=None)
        dem.feed(impair.to_pcm(np.random.default_rng(1).normal(0, 3000, 8000)))
        self.assertFalse(dem.dcd)
        mod = AfskModulator(BELL202)
        dem.feed(mod.modulate_bits([0, 1, 1, 1, 1, 1, 1, 0] * 40))
        self.assertTrue(dem.dcd)
        dem.feed(np.zeros(800, np.int16))
        self.assertFalse(dem.dcd, "DCD must drop when the signal stops")

    def test_dcd_energy(self):
        dem = AfskDemodulator(BELL202)
        dem.feed(np.zeros(160, np.int16))
        self.assertFalse(dem.dcd)
        dem.feed(AfskModulator(BELL202).modulate_bits([0, 1, 1, 1, 1, 1, 1, 0] * 3)[:160])
        self.assertTrue(dem.dcd, "in-band energy is busy within one block")
        dem.feed(np.zeros(160, np.int16))
        dem.feed(np.zeros(800, np.int16))
        self.assertFalse(dem.dcd)

    def test_realtime_margin(self):
        audio = impair.to_pcm(transmit(BELL202, make_frames(5)))
        dem = AfskDemodulator(BELL202)
        t = time.perf_counter()
        for i in range(0, len(audio), 160):
            dem.feed(audio[i:i + 160])
        self.assertLess((time.perf_counter() - t) / (len(audio) / 8000), 0.25)


if __name__ == "__main__":
    unittest.main()
