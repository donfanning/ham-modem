import random
import unittest

import numpy as np

from hammodem import kiss
from hammodem.ax25 import Frame
from hammodem.tnc import Tnc

N = 160


class Station:
    def __init__(self, seed, **kw):
        self.tnc = Tnc("afsk1200", 8000, rng=random.Random(seed), slottime=0.05, txdelay=0.15, **kw)
        self.wire = bytearray()
        self.ep = self.tnc.attach(self.wire.extend, f"st{seed}")
        self.dec = kiss.Decoder()

    def received(self):
        return [d for _, c, d in self.dec.feed(bytes(self.wire)) if c == kiss.DATA]


def run_bridge(stations, seconds):
    """Mix like ConfBridge: each station hears the sum of the others, never itself."""
    for _ in range(int(seconds * 50)):
        out = [s.tnc.audio_out(N).astype(np.int32) for s in stations]
        total = sum(out)
        for s, own in zip(stations, out):
            s.tnc.audio_in(np.clip(total - own, -32768, 32767).astype(np.int16))


def ui(src, i):
    return Frame.ui(src, "APRS", f">status {i} from {src}", "").encode()


class TncTest(unittest.TestCase):
    def test_kiss_in_audio_out_kiss_out(self):
        a, b = Station(1), Station(2)
        frame = ui("N0CALL", 0)
        a.ep.feed(kiss.encode(frame))
        run_bridge([a, b], 2)
        self.assertEqual(b.received(), [frame])
        self.assertEqual(a.received(), [])          # no echo of our own transmission

    def test_kiss_parameters(self):
        a = Station(1)
        a.ep.feed(kiss.encode(bytes((25,)), command=kiss.TXDELAY) + kiss.encode(bytes((200,)), command=kiss.PERSIST)
                  + kiss.encode(bytes((5,)), command=kiss.SLOTTIME) + kiss.encode(b"\x01", command=kiss.FULLDUPLEX))
        ch = a.tnc.channel
        self.assertEqual((ch.txdelay, ch.persist, ch.slottime, ch.fullduplex), (0.25, 200, 0.05, True))

    def test_three_stations_csma_all_delivered(self):
        stations = [Station(s) for s in range(3)]
        sent = {}
        for i, st in enumerate(stations):
            sent[i] = [ui(f"N{i}CALL", k) for k in range(4)]
            for f in sent[i]:
                st.ep.feed(kiss.encode(f))
        run_bridge(stations, 25)
        for i, st in enumerate(stations):
            expected = sorted(f for j, fs in sent.items() if j != i for f in fs)
            self.assertEqual(sorted(st.received()), expected, f"station {i}")
            self.assertEqual(len(st.tnc.channel.queue), 0)


if __name__ == "__main__":
    unittest.main()
