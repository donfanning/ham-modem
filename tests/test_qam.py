import unittest

import numpy as np

from hammodem.ax25 import Frame
from hammodem.modems import make
from hammodem.modems.qam import Constellation, QamDemodulator, QamModulator
from tests import impair


def bursts(name, frames, gap=1600):
    mod, _ = make(name, 16000)
    return np.concatenate([np.concatenate((np.zeros(gap, np.int16), mod.modulate([f]))) for f in frames]
                          + [np.zeros(gap, np.int16)]).astype(float)


def receive(name, audio, block=160):
    _, dem = make(name, 16000)
    pcm = impair.to_pcm(audio)
    got = []
    for i in range(0, len(pcm), block):
        got += dem.feed(pcm[i:i + block])
    return got


class QamTest(unittest.TestCase):
    def frames(self, n=6):
        return [Frame.ui("N0CALL-1", "APRS", f">qam {i} " + "x" * 200).encode() for i in range(n)]

    def check(self, name, fn, minimum):
        fs = self.frames()
        got = receive(name, fn(bursts(name, fs), np.random.default_rng(4)))
        self.assertEqual(len(got), sum(g in fs for g in got))
        self.assertGreaterEqual(sum(g in fs for g in got), minimum)

    def test_constellation_gray_and_unit_energy(self):
        for bits in (6, 8):
            c = Constellation(bits)
            pts = np.array([a + 1j * b for a in c.levels for b in c.levels])
            self.assertAlmostEqual(float(np.mean(np.abs(pts) ** 2)), 1.0, places=9)
            order = [c.index_to_gray[i] for i in range(c.L)]
            self.assertTrue(all(bin(a ^ b).count("1") == 1 for a, b in zip(order, order[1:])))

    def test_51200_clean_noise_gain_drift(self):
        self.check("qam51200", lambda a, r: a, 6)
        self.check("qam51200", lambda a, r: impair.noise(a, 100, r), 6)
        self.check("qam51200", lambda a, r: a * 0.3, 6)
        for ppm in (-300, 300):
            self.check("qam51200", lambda a, r: impair.drift_bandlimited(a, ppm), 6)

    def test_38400_noise_and_drift(self):
        self.check("qam38400", lambda a, r: impair.noise(impair.drift_bandlimited(a, 200), 150, r), 6)

    def test_multi_frame_burst(self):
        mod, _ = make("qam51200", 16000)
        fs = self.frames(7)
        audio = np.concatenate((np.zeros(800, np.int16), mod.modulate(fs), np.zeros(1600, np.int16))).astype(float)
        self.assertEqual(receive("qam51200", impair.drift_bandlimited(audio, -300)), fs)

    def test_block_size_independent(self):
        fs = self.frames(3)
        audio = bursts("qam51200", fs)
        for block in (37, 160, 1000):
            self.assertEqual(receive("qam51200", audio, block), fs)

    def test_noise_alone_gives_nothing(self):
        self.assertEqual(receive("qam51200", np.random.default_rng(1).normal(0, 3000, 32000)), [])

    def test_only_16k(self):
        with self.assertRaises(ValueError):
            QamModulator(48000)
        with self.assertRaises(ValueError):
            QamDemodulator(8000)


if __name__ == "__main__":
    unittest.main()
