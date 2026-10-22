import unittest

import numpy as np

from hammodem.ax25 import Frame
from hammodem.modems import make
from tests import impair


def bursts(name, frames, fs=8000, gap=2000):
    mod, _ = make(name, fs)
    return np.concatenate([np.concatenate((mod.modulate([f]), np.zeros(gap, np.int16))) for f in frames]).astype(float)


def receive(name, audio, fs=8000, block=160):
    _, dem = make(name, fs)
    pcm = impair.to_pcm(audio)
    got = []
    for i in range(0, len(pcm), block):
        got += dem.feed(pcm[i:i + block])
    return got


def frames(n=8):
    return [Frame.ui("N0CALL-1", "APRS", f">psk {i} " + "x" * 60, "WIDE1-1").encode() for i in range(n)]


class PskTest(unittest.TestCase):
    def check(self, name, fn, minimum, fs=8000, gap=2000):
        fs_ = frames()
        got = receive(name, fn(bursts(name, fs_, fs, gap), np.random.default_rng(3)), fs)
        self.assertEqual(len(got), sum(g in fs_ for g in got))
        self.assertGreaterEqual(sum(g in fs_ for g in got), minimum)

    def test_qpsk_clean_noise_drift_gain(self):
        self.check("psk2400", lambda a, r: a, 8)
        self.check("psk2400", lambda a, r: impair.noise(a, 2000, r), 8)       # 3000 rms is the 8 kHz edge
        self.check("psk2400", lambda a, r: impair.drift_bandlimited(a, 300), 8)
        self.check("psk2400", lambda a, r: a * 0.3, 8)

    def test_8psk_clean_and_noise(self):
        self.check("psk4800", lambda a, r: a, 8)
        self.check("psk4800", lambda a, r: impair.noise(a, 1000, r), 7)

    def test_short_gaps_between_packets(self):
        # Direwolf leaves ~10 ms between packets: re-acquisition must not wait for a whole quiet block.
        self.check("psk4800", lambda a, r: a, 8, fs=48000, gap=480)

    def test_noise_filled_gaps(self):
        # With noise in the gaps the onset is relative to the noise floor (and quality-triggered).
        self.check("psk4800", lambda a, r: impair.noise(a, 800, r), 7, gap=2000)

    def test_48k(self):
        self.check("psk2400", lambda a, r: a, 8, fs=48000)


if __name__ == "__main__":
    unittest.main()
