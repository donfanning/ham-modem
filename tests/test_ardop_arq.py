import unittest

import numpy as np

from hammodem.ardop.arq import ArqStation, negotiate_bw

FS = 8000
BLK = FS // 50


def pair(bw_a="500MAX", bw_b="500MAX", **kw):
    return (ArqStation("N0CALL", fs=FS, bandwidth=bw_a, seed=1, **kw),
            ArqStation("K1ABC", fs=FS, bandwidth=bw_b, seed=2, **kw))


def run(a, b, seconds, until=None, drop=0.0, seed=0):
    """Clock both stations through a back-to-back channel; ``drop`` silently loses whole transmissions."""
    rng = np.random.default_rng(seed)
    ta = tb = np.zeros(BLK)
    keyed, kill = [False, False], [False, False]
    for _ in range(int(seconds * 50)):
        for i, x in enumerate((ta, tb)):
            on = bool(np.any(x))
            if on and not keyed[i]:
                kill[i] = rng.random() < drop
            keyed[i] = on
        ra = np.zeros(BLK) if kill[1] else tb
        rb = np.zeros(BLK) if kill[0] else ta
        ta, tb = a.process(ra).astype(float), b.process(rb).astype(float)
        if until and until():
            return True
    return False


class NegotiationTest(unittest.TestCase):
    def test_ardopcf_rules(self):
        c = {"200M": 0x31, "500M": 0x32, "1000M": 0x33, "2000M": 0x34, "200F": 0x35, "500F": 0x36,
             "1000F": 0x37, "2000F": 0x38}
        self.assertEqual(negotiate_bw("500MAX", c["2000M"]), 500)
        self.assertEqual(negotiate_bw("500MAX", c["200F"]), 200)
        self.assertIsNone(negotiate_bw("500MAX", c["1000F"]))
        self.assertEqual(negotiate_bw("2000MAX", c["1000M"]), 1000)
        self.assertEqual(negotiate_bw("500FORCED", c["2000M"]), 500)
        self.assertIsNone(negotiate_bw("500FORCED", c["200M"]))
        self.assertIsNone(negotiate_bw("500FORCED", c["1000F"]))
        self.assertEqual(negotiate_bw("200MAX", c["200F"]), 200)


class SessionTest(unittest.TestCase):
    def test_connect_transfer_turnaround_disconnect(self):
        a, b = pair()
        data, reply = bytes(range(256)) * 4, b"73 de K1ABC" * 10
        a.send(data)
        a.connect("K1ABC")
        self.assertTrue(run(a, b, 60, until=lambda: len(b.received) == len(data)))
        self.assertIn("CONNECTED K1ABC 500", a.events)
        self.assertIn("CONNECTED N0CALL 500", b.events)
        b.send(reply)                                        # B takes over with BREAK when A goes IDLE
        self.assertTrue(run(a, b, 60, until=lambda: len(a.received) == len(reply)))
        self.assertIn("NEWSTATE IRStoISS", b.events)
        a.disconnect()
        self.assertTrue(run(a, b, 30, until=lambda: a.state == b.state == "DISC" and not a.keyed))
        self.assertEqual((bytes(b.received), bytes(a.received)), (data, reply))
        self.assertIn("DISCONNECTED", a.events)
        self.assertIn("DISCONNECTED", b.events)
        self.assertGreater(a.stats["shift_up"], 0)

    def test_irs_disconnect_while_receiving_data(self):
        a, b = pair()
        a.send(bytes(range(256)) * 6)
        a.connect("K1ABC")
        self.assertTrue(run(a, b, 60, until=lambda: len(b.received) > 0))
        b.disconnect()                                       # IRS: DISC goes out in place of the next ACK
        self.assertTrue(run(a, b, 30, until=lambda: a.state == b.state == "DISC" and not b.keyed))
        self.assertIn("DISCONNECTED", a.events)

    def test_irs_disconnect_after_the_iss_went_silent(self):
        a, b = pair()
        a.send(b"x" * 100)
        a.connect("K1ABC")
        self.assertTrue(run(a, b, 60, until=lambda: len(b.received) == 100))
        a.abort()                                            # ISS vanishes without a word
        b.disconnect()
        self.assertTrue(run(a, b, 30, until=lambda: b.state == "DISC"))

    def test_lost_transmissions_are_repeated_without_duplicates(self):
        a, b = pair()
        data = bytes(np.random.default_rng(3).integers(0, 256, 900, dtype=np.uint8))
        a.send(data)
        a.connect("K1ABC")
        self.assertTrue(run(a, b, 200, until=lambda: len(b.received) >= len(data), drop=0.25, seed=5))
        self.assertEqual(bytes(b.received), data)
        self.assertGreater(a.stats["repeats"], 0)

    def test_incompatible_bandwidth_is_rejected(self):
        a, b = pair("2000FORCED", "500FORCED")
        a.connect("K1ABC")
        self.assertTrue(run(a, b, 20, until=lambda: a.state == "DISC"))
        self.assertIn("REJECTEDBW K1ABC", a.events)
        self.assertEqual(b.state, "DISC")

    def test_unanswered_call_gives_up_after_five_tries(self):
        a, b = pair()
        b.listen = False
        a.connect("K1ABC")
        self.assertTrue(run(a, b, 40, until=lambda: a.state == "DISC"))
        self.assertIn("STATUS CONNECT TO K1ABC FAILED!", a.events)

    def test_ping(self):
        a, b = pair()
        a.ping("K1ABC", 3)
        self.assertTrue(run(a, b, 15, until=lambda: any(e.startswith("PINGACK") for e in a.events)))
        self.assertIn("PINGREPLY", b.events)


if __name__ == "__main__":
    unittest.main()
