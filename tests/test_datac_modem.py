import unittest

import numpy as np

from hammodem.modems import make
from hammodem.modems.datac import Reassembler, number, segment

try:
    from hammodem.freedata.codec2 import Datac
    Datac("datac0").close()
    HAVE = True
except OSError:
    HAVE = False


def frames(*sizes):
    return [bytes((i * 17 + k) % 256 for k in range(n)) for i, n in enumerate(sizes)]


def reassemble(chunks):
    asm, out = Reassembler(), []
    for burst in number(chunks):
        for c in burst:
            out += asm.chunk(c)
    return out


class SegmentTest(unittest.TestCase):
    def test_small_frames_pack_into_one_chunk(self):
        fs = frames(20, 30, 40)
        chunks = segment(fs, 126)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(reassemble(chunks), fs)

    def test_long_frames_span_chunks(self):
        fs = frames(330, 15, 100)
        for cap in (14, 54, 126, 510):
            chunks = segment(fs, cap)
            self.assertTrue(all(len(c) == cap for c in chunks))
            self.assertEqual(reassemble(chunks), fs, cap)

    def test_a_lost_chunk_loses_only_what_it_touched(self):
        fs = frames(100, 100, 100)
        chunks = [c for b in number(segment(fs, 54)) for c in b]
        asm, out = Reassembler(), []
        for i, c in enumerate(chunks):
            if i == 1:                                  # inside the first frame
                asm.reset()
                continue
            out += asm.chunk(c)
        self.assertEqual(out, fs[1:])


@unittest.skipUnless(HAVE, "libcodec2 not installed")
class ModemTest(unittest.TestCase):
    def over_the_air(self, mode, fs, cut=None):
        mod, dem = make(mode, 8000)
        x = mod.modulate(fs, 0.1, 0.05)
        if cut:
            x = x.copy()
            x[cut[0]:cut[1]] = 0
        got = []
        x = np.concatenate((np.zeros(777, np.int16), x, np.zeros(12000, np.int16)))
        for i in range(0, len(x), 160):
            got += dem.feed(x[i:i + 160])
        return got, dem

    def test_datac3_ax25_frames(self):
        fs = frames(60, 200, 330)
        got, dem = self.over_the_air("datac3", fs)
        self.assertEqual(got, fs)

    def test_datac0_spans_many_chunks_and_bursts(self):
        fs = frames(70, 45)                             # 9 chunks of 14 bytes: two bursts of at most 8
        got, dem = self.over_the_air("datac0", fs)
        self.assertEqual(got, fs)

    def test_a_short_burst_does_not_hide_the_next_one(self):
        mod, dem = make("datac3", 8000)
        first, second, third = frames(30), frames(300), frames(35)
        x = np.concatenate((np.zeros(500, np.int16), mod.modulate(first, 0.1, 0.05), np.zeros(4000, np.int16),
                            mod.modulate(second, 0.1, 0.05), mod.modulate(third, 0.1, 0.05), np.zeros(12000, np.int16)))
        got = []
        for i in range(0, len(x), 160):
            got += dem.feed(x[i:i + 160])
        self.assertEqual(got, first + second + third)

    def test_repeated_rounds_are_one_frame_but_a_later_retransmission_is_not(self):
        mod, dem = make("datac3", 8000)
        fs_ = frames(200)
        once = mod.modulate(fs_, 0.1, 0.25)
        x = np.concatenate((np.zeros(500, np.int16), once, mod.modulate(fs_, 0.0, 0.05), np.zeros(20000, np.int16),
                            mod.modulate(fs_, 0.1, 0.05), np.zeros(12000, np.int16)))
        got = []
        for i in range(0, len(x), 160):
            got += dem.feed(x[i:i + 160])
        self.assertEqual(got, fs_ + fs_)                 # round 1+2 once, the retransmission after silence again

    def test_datac13_and_datac4(self):
        for mode in ("datac13", "datac4"):
            fs = frames(40)
            got, _ = self.over_the_air(mode, fs)
            self.assertEqual(got, fs, mode)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE, "libcodec2 not installed")
class TncTest(unittest.TestCase):
    def test_two_tncs_exchange_ui_frames_and_connected_mode_ready(self):
        from hammodem.ax25 import Frame
        from hammodem.tnc import Tnc
        a, b = Tnc("datac3", 8000, fullduplex=True), Tnc("datac3", 8000, fullduplex=True)
        heard = []
        b.attach(lambda data: heard.append(data), "b")
        ui = Frame.ui("K1ABC", "N0CALL", b"hello over datac3 " * 8).encode()
        a.send(ui)
        for _ in range(50 * 15):
            b.audio_in(a.audio_out(160))
            a.audio_in(b.audio_out(160))
        self.assertGreaterEqual(b.heard, 1)
        self.assertTrue(any(ui[:14] in d for d in heard))
