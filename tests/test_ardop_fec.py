import unittest

import numpy as np

from hammodem.ardop.arq import ArqStation
from tests.test_ardop_arq import BLK, FS
from tests.test_ardop_host import Bench


def broadcast(tx, rxs, seconds, drop=0.0, seed=0, until=None):
    """One-way channel: every receiver hears the sender; ``drop`` loses whole 20 ms blocks."""
    rng = np.random.default_rng(seed)
    out = np.zeros(BLK)
    for _ in range(int(seconds * 50)):
        for rx in rxs:
            rx.process(np.zeros(BLK) if rng.random() < drop else out)
        out = tx.process(np.zeros(BLK)).astype(float)
        if until and until():
            return True
    return False


def station(call, got):
    st = ArqStation(call, fs=FS, seed=len(call), on_data=lambda tag, d: got.append((tag, d)))
    st.protocol_mode = "FEC"
    return st


class FecTest(unittest.TestCase):
    def test_broadcast_reaches_every_listener(self):
        got_a, got_b = [], []
        tx, a, b = station("N0CALL", []), station("K1ABC", got_a), station("W1AW", got_b)
        data = bytes(range(256)) * 3
        tx.send(data)
        self.assertTrue(tx.fec_start())
        self.assertTrue(broadcast(tx, [a, b], 60, until=lambda: tx.state == "DISC"))
        for got in (got_a, got_b):
            self.assertEqual(b"".join(d for t, d in got if t == "FEC"), data)
        self.assertIn("NEWSTATE FECSEND", tx.events)

    def test_repeats_are_dropped_by_the_receiver_and_cover_losses(self):
        got = []
        tx, rx = station("N0CALL", []), station("K1ABC", got)
        tx.fec_repeats = 2
        data = bytes(np.random.default_rng(1).integers(0, 256, 330, dtype=np.uint8))
        tx.send(data)
        tx.fec_start()
        broadcast(tx, [rx], 100, drop=0.04, seed=3, until=lambda: tx.state == "DISC")
        broadcast(tx, [rx], 2)
        self.assertEqual(b"".join(d for t, d in got if t == "FEC"), data)

    def test_other_mode_and_id(self):
        got = []
        tx, rx = station("N0CALL", []), station("K1ABC", got)
        tx.fec_mode, tx.fec_id = "4PSK.500.100", True
        tx.send(b"CQ CQ de N0CALL " * 10)
        tx.fec_start()
        self.assertTrue(broadcast(tx, [rx], 60, until=lambda: tx.state == "DISC"))
        self.assertEqual(b"".join(d for t, d in got if t == "FEC"), b"CQ CQ de N0CALL " * 10)
        self.assertTrue(any(t == "IDF" for t, _ in got))

    def test_nothing_queued_is_refused(self):
        self.assertFalse(station("N0CALL", []).fec_start())


class FecHostTest(unittest.TestCase):
    def test_host_commands_and_data_tags(self):
        bench = Bench()
        a, b = bench.a, bench.b
        try:
            for c, call in ((a, "N0CALL"), (b, "K1ABC")):
                for text in ("INITIALIZE", f"MYCALL {call}", "PROTOCOLMODE FEC"):
                    c.cmd(text)
            a.cmd("FECMODE 4FSK.500.100")
            a.cmd("FECREPEATS 1")
            a.cmd("FECID FALSE")
            a.cmd("FECSEND TRUE")
            bench.step(3)
            self.assertIn("PROTOCOLMODE now FEC", a.lines)
            self.assertIn("FECMODE now 4FSK.500.100", a.lines)
            self.assertIn("FECREPEATS now 1", a.lines)
            self.assertIn("FAULT StartFEC failed for FECSEND TRUE.", a.lines)    # nothing queued yet
            a.send(b"Hello list " * 20)
            bench.step(3)
            a.cmd("FECSEND TRUE")
            self.assertTrue(bench.until(lambda: len(b.data.get("FEC", b"")) >= 220, 60))
            self.assertEqual(bytes(b.data["FEC"]), b"Hello list " * 20)
            self.assertIn("FECSEND now TRUE", a.lines)
            a.cmd("ARQCALL K1ABC 5")
            bench.step(3)
            self.assertIn("FAULT Not from mode FEC", a.lines)
        finally:
            bench.close()


if __name__ == "__main__":
    unittest.main()
