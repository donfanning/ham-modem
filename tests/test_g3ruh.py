import unittest

import numpy as np

from hammodem.modems.g3ruh import G3ruhDemodulator, G3ruhModulator, Scrambler
from tests import impair
from tests.test_afsk import make_frames


def transmit(frames, fs):
    mod = G3ruhModulator(fs)
    return np.concatenate([np.concatenate((mod.modulate([f]), np.zeros(fs // 10, np.int16))) for f in frames]
                          ).astype(float)


def receive(audio, fs, block=None):
    dem, got = G3ruhDemodulator(fs), []
    pcm = impair.to_pcm(audio)
    block = block or fs // 50
    for i in range(0, len(pcm), block):
        got += dem.feed(pcm[i:i + block])
    return got, dem


class G3ruhTest(unittest.TestCase):
    def check(self, fs, fn, minimum, n=20):
        frames = make_frames(n)
        got, _ = receive(fn(transmit(frames, fs), np.random.default_rng(5)), fs)
        self.assertEqual(len(got), sum(g in frames for g in got))
        self.assertGreaterEqual(sum(g in frames for g in got), minimum)

    def test_scrambler_self_synchronises(self):
        bits = [1, 0, 1, 1, 0, 0, 0, 1] * 20
        scr = Scrambler().scramble(bits)
        s, out = 0x1ABCD, []                          # receiver starts in an arbitrary state
        for b in scr:
            out.append((b ^ (s >> 16) ^ (s >> 11)) & 1)
            s = ((s << 1) | b) & 0x1FFFF
        self.assertEqual(out[17:], bits[17:])

    def test_16k_noise_and_drift(self):
        self.check(16000, lambda a, r: impair.noise(impair.drift(a, 300), 2000, r), 20)

    def test_48k_noise(self):
        self.check(48000, lambda a, r: impair.noise(a, 3000, r), 20)

    def test_dc_offset_and_inversion(self):
        self.check(16000, lambda a, r: -a + 3000, 20)

    def test_block_size_independent(self):
        frames = make_frames(3)
        audio = transmit(frames, 16000)
        for block in (7, 320, 5000):
            self.assertEqual(receive(audio, 16000, block)[0], frames)

    def test_refuses_narrowband(self):
        with self.assertRaises(ValueError):
            G3ruhDemodulator(8000)
        with self.assertRaises(ValueError):
            G3ruhModulator(44100)

    def test_dcd(self):
        dem = G3ruhDemodulator(16000, energy_dcd_dbfs=None)
        dem.feed(impair.to_pcm(np.random.default_rng(1).normal(0, 3000, 16000)))
        self.assertFalse(dem.dcd)
        dem.feed(G3ruhModulator(16000).modulate_bits([0, 1, 1, 1, 1, 1, 1, 0] * 200))
        self.assertTrue(dem.dcd)
        dem.feed(np.zeros(4000, np.int16))
        self.assertFalse(dem.dcd)


if __name__ == "__main__":
    unittest.main()
