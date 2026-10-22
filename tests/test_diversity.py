import random
import unittest

import numpy as np

from hammodem.ax25 import Frame
from hammodem.channel import Channel
from hammodem.modems import make


def channel_audio(name, fs, frames, repeat):
    mod, _ = make(name, fs)
    ch = Channel(mod, fs, persist=255, repeat=repeat, txdelay=0.05, rng=random.Random(1))
    for f in frames:
        ch.send(f)
    blk = fs // 50
    out = []
    for _ in range(400):
        out.append(ch.tx_samples(blk, False))
    return np.concatenate(out), blk


class DiversityTest(unittest.TestCase):
    def test_repeat_survives_any_single_lost_packet(self):
        fs = 48000
        frames = [Frame.ui("N0CALL", "APRS", f">div {i} " + "q" * 200).encode() for i in range(2)]
        audio, blk = channel_audio("pam57600", fs, frames, repeat=2)
        blocks = audio.reshape(-1, blk)
        busy = [i for i, b in enumerate(blocks) if b.any()]
        for lose in busy[::max(1, len(busy) // 6)]:         # lose packets spread across the burst, one at a time
            _, dem = make("pam57600", fs)
            got, last = [], np.zeros(blk, np.int16)
            for i, b in enumerate(blocks):
                if i == lose:
                    got += dem.feed(last.copy(), erased=True)
                else:
                    got += dem.feed(b)
                    last = b
            self.assertEqual(sorted(got), sorted(frames), f"lost packet {lose}")

    def test_single_copy_loses_a_frame(self):
        fs = 48000
        frames = [Frame.ui("N0CALL", "APRS", ">once " + "q" * 200).encode()]
        audio, blk = channel_audio("pam57600", fs, frames, repeat=1)
        blocks = audio.reshape(-1, blk)
        busy = [i for i, b in enumerate(blocks) if b.any()]
        lose = busy[len(busy) // 2 + 1]
        _, dem = make("pam57600", fs)
        got, last = [], np.zeros(blk, np.int16)
        for i, b in enumerate(blocks):
            if i == lose:
                got += dem.feed(last.copy(), erased=True)
            else:
                got += dem.feed(b)
                last = b
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
