import unittest

import numpy as np

from hammodem.amtor import code
from hammodem.amtor.arq import ArqStation, selcal

BLK = 160


def link(a, b, seconds, delay=0, drop=0.0, noise=0.0, seed=0, until=None):
    """Two stations on an audio path (``delay`` samples each way on top of one block). ``drop`` is the probability
    that a whole burst is lost; ``noise`` is the sigma of added white noise (burst level is about 7000 rms)."""
    rng = np.random.default_rng(seed)
    qa, qb = [np.zeros(BLK)] * (delay // BLK + 1), [np.zeros(BLK)] * (delay // BLK + 1)
    tail_on, kill = [False, False], [False, False]
    for blk in range(int(seconds * 8000 / BLK)):
        ra, rb = qb.pop(0), qa.pop(0)
        if noise:
            ra = ra + rng.normal(0, noise, BLK)
            rb = rb + rng.normal(0, noise, BLK)
        oa, ob = a.process(ra).astype(float), b.process(rb).astype(float)
        for i, o in enumerate((oa, ob)):
            if np.any(o) and not tail_on[i]:
                kill[i] = rng.random() < drop
            if kill[i]:
                o[:] = 0
            tail_on[i] = o[-1] != 0 or (kill[i] and tail_on[i] and False)
        qa.append(oa)
        qb.append(ob)
        if until and until():
            return True
    return False


def pair():
    got = {"A": "", "B": ""}
    a = ArqStation("W1AW", on_text=lambda s: got.__setitem__("A", got["A"] + s))
    b = ArqStation("G3PLX", on_text=lambda s: got.__setitem__("B", got["B"] + s))
    return a, b, got


class SelcalTest(unittest.TestCase):
    def test_amateur_convention(self):
        self.assertEqual(selcal("G3PLX"), "GPLX")
        self.assertEqual(selcal("W1AW"), "WWAW")
        self.assertEqual(selcal("N0CALL"), "NCAL")


def settle(a, b, got, text="HELLO FROM W1AW 1234 ", **kw):
    a.send(text)
    a.connect("G3PLX")
    assert link(a, b, 90, until=lambda: text.strip() in got["B"] and b.state == "irs" and not a.queue, **kw)


class LinkTest(unittest.TestCase):
    def test_call_traffic_changeover_and_end(self):
        a, b, got = pair()
        settle(a, b, got)
        self.assertEqual((a.state, b.state), ("iss", "irs"))
        b.send("REPLY OK ")
        a.turn()
        self.assertTrue(link(a, b, 30, until=lambda: "REPLY OK" in got["A"]))
        self.assertEqual((a.state, b.state), ("irs", "iss"))
        b.disconnect()
        self.assertTrue(link(a, b, 20, until=lambda: a.state == b.state == "standby"))

    def test_slave_can_send_first_after_being_called_and_master_ends(self):
        a, b, got = pair()
        settle(a, b, got, "X ")
        a.disconnect()
        self.assertTrue(link(a, b, 20, until=lambda: a.state == b.state == "standby"))
        self.assertIn("STANDBY (ended)", a.events)

    def test_unanswered_call_gives_up(self):
        a, b, got = pair()
        b.listen = False
        a.connect("G3PLX")
        link(a, b, 20)
        self.assertEqual(a.state, "standby")
        self.assertIn("STANDBY (no answer)", a.events)

    def test_lost_bursts_are_repeated(self):
        a, b, got = pair()
        text = "THE QUICK BROWN FOX JUMPS OVER THE LAZY DOG 0123456789 "
        a.send(text)
        a.connect("G3PLX")
        self.assertTrue(link(a, b, 200, drop=0.2, seed=4, until=lambda: text.strip() in got["B"]))
        self.assertEqual(got["B"].strip(), text.strip())
        self.assertGreater(a.stats["repeats"] + a.stats["rq"], 0)

    def test_noise_and_delay(self):
        a, b, got = pair()
        text = "RYRYRY DE W1AW "
        a.send(text)
        a.connect("G3PLX")
        self.assertTrue(link(a, b, 120, delay=480, noise=2500, seed=2, until=lambda: text.strip() in got["B"]))


if __name__ == "__main__":
    unittest.main()
