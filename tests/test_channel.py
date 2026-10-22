import random
import unittest

import numpy as np

from hammodem.channel import Channel
from hammodem.modems.afsk import BELL202, AfskModulator

N = 160


class ChannelTest(unittest.TestCase):
    def channel(self, **kw):
        kw.setdefault("rng", random.Random(1))
        return Channel(AfskModulator(BELL202), 8000, **kw)

    def run_blocks(self, ch, blocks, dcd=lambda i: False):
        return [ch.tx_samples(N, dcd(i)) for i in range(blocks)]

    def test_idle_is_silence_of_exact_length(self):
        out = self.run_blocks(self.channel(), 5)
        self.assertTrue(all(len(b) == N and not b.any() for b in out))

    def test_sends_with_persist_255(self):
        ch = self.channel(persist=255, txdelay=0.1)
        ch.send(b"A" * 20)
        out = self.run_blocks(ch, 60)
        self.assertTrue(out[0].any())
        self.assertEqual((ch.bursts, ch.frames_sent), (1, 1))
        self.assertFalse(out[-1].any())

    def test_defers_while_busy(self):
        ch = self.channel(persist=255)
        ch.send(b"A" * 20)
        out = self.run_blocks(ch, 50, dcd=lambda i: i < 40)
        self.assertFalse(any(b.any() for b in out[:40]))
        self.assertTrue(any(b.any() for b in out[40:]))
        self.assertGreater(ch.deferrals, 0)

    def test_full_duplex_ignores_dcd(self):
        ch = self.channel(persist=0, fullduplex=True)
        ch.send(b"A" * 20)
        self.assertTrue(self.run_blocks(ch, 1, dcd=lambda i: True)[0].any())

    def test_persistence_spreads_access(self):
        delays = []
        for seed in range(30):
            ch = self.channel(persist=63, slottime=0.02, rng=random.Random(seed))
            ch.send(b"A" * 20)
            out = self.run_blocks(ch, 200)
            delays.append(next(i for i, b in enumerate(out) if b.any()))
        self.assertGreater(len(set(delays)), 5)

    def test_maxframe_bursts(self):
        ch = self.channel(persist=255, maxframe=3)
        for _ in range(5):
            ch.send(b"A" * 20)
        self.run_blocks(ch, 400)
        self.assertEqual((ch.bursts, ch.frames_sent), (2, 5))


if __name__ == "__main__":
    unittest.main()
