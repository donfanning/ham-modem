import unittest
from unittest import mock

import numpy as np

from hammodem.ardop import arq
from hammodem.ardop.arq import ArqStation
from tests.test_ardop_arq import BLK, FS, pair, run


def tone(freq, seconds, amp=3000):
    t = np.arange(int(seconds * FS)) / FS
    return amp * np.sin(2 * np.pi * freq * t)


def feed(st, x):
    for i in range(0, len(x) - BLK + 1, BLK):
        st.process(x[i:i + BLK])


class BusyDetectTest(unittest.TestCase):
    def test_carrier_trips_busy_and_holds_five_seconds(self):
        st = ArqStation("N0CALL", fs=FS, seed=1)
        rng = np.random.default_rng(0)
        noise = lambda s: rng.normal(0, 30, int(s * FS))
        feed(st, noise(2))
        self.assertFalse(st.busy.busy)
        feed(st, tone(1500, 1.5) + noise(1.5))
        self.assertTrue(st.busy.busy)
        self.assertIn("BUSY TRUE", st.events)
        feed(st, noise(3))
        self.assertTrue(st.busy.busy)                       # still held
        feed(st, noise(5))
        self.assertFalse(st.busy.busy)
        self.assertIn("BUSY FALSE", st.events)

    def test_busydet_zero_disables(self):
        st = ArqStation("N0CALL", fs=FS, seed=1)
        st.busy.level = 0
        feed(st, tone(1500, 2))
        self.assertFalse(st.busy.busy)


class BusyBlockTest(unittest.TestCase):
    def test_call_into_a_busy_channel_is_rejected(self):
        a, b = pair()
        b.busy_block = True
        rng = np.random.default_rng(1)
        carrier = tone(1600, 30, 1500) + rng.normal(0, 30, 30 * FS)    # someone else is on the channel
        feed(b, carrier[:2 * FS])
        self.assertTrue(b.busy.busy)
        a.connect("K1ABC")
        ta = tb = np.zeros(BLK)
        for i in range(2 * 50, 30 * 50):
            ta = a.process(tb).astype(float)
            tb = b.process(ta + carrier[i * BLK:(i + 1) * BLK]).astype(float)
            if any(e.startswith("REJECTEDBUSY") for e in a.events):
                break
        self.assertTrue(any(e.startswith("REJECTEDBUSY") for e in b.events), b.events)
        self.assertTrue(any(e.startswith("REJECTEDBUSY") for e in a.events), a.events)


class TenMinuteIdTest(unittest.TestCase):
    def test_id_frame_is_sent_during_a_long_session(self):
        with mock.patch.object(arq, "ID_DUE_MS", 20_000):
            a, b = pair()
            ids = []
            b.on_data = lambda tag, d: ids.append(d) if tag == "IDF" else None
            a.send(bytes(range(256)) * 12)
            a.connect("K1ABC")
            run(a, b, 150, until=lambda: len(b.received) == 3072)
            self.assertEqual(len(b.received), 3072)
            self.assertTrue(ids, "no ID frame heard during the session")
            self.assertTrue(any(b"N0CALL" in i for i in ids))


if __name__ == "__main__":
    unittest.main()
