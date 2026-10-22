import random
import unittest

from hammodem.ax25 import Frame
from hammodem.ax25_link import CONNECTED, DISCONNECTED, LinkManager, LinkParams


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Net:
    """Frames sent by one manager are heard by the others after one step; optional loss and a digipeater."""

    def __init__(self, loss=0.0, seed=1, digi=None):
        self.clock, self.loss, self.rng, self.digi = Clock(), loss, random.Random(seed), digi
        self.mgrs, self.air, self.sent = [], [], 0

    def station(self, call, **kw):
        m = LinkManager(call, self.air.append, LinkParams(frack=2.0, t2=0.2, **kw), clock=self.clock)
        m.events, m.data = [], {}
        m.on_connect = lambda lk, inc, m=m: m.events.append(("connect", lk.name, inc))
        m.on_disconnect = lambda lk, why, m=m: m.events.append(("disconnect", lk.name, why))
        m.on_data = lambda lk, d, m=m: m.data.setdefault(lk.name, bytearray()).extend(d)
        self.mgrs.append(m)
        return m

    def run(self, seconds, step=0.1):
        for _ in range(int(seconds / step)):
            frames, self.air[:] = list(self.air), []
            for raw in frames:
                self.sent += 1
                if self.rng.random() < self.loss:
                    continue
                f = Frame.decode(raw)
                if self.digi and f.digis and not f.digis[0].flag and f.digis[0].call == self.digi:
                    f.digis[0].flag = True                    # the digipeater repeats it
                    self.air.append(f.encode())
                    continue
                for m in self.mgrs:
                    m.receive(f)
            self.clock.t += step
            for m in self.mgrs:
                m.tick()


class LinkTest(unittest.TestCase):
    def test_connect_exchange_disconnect(self):
        net = Net()
        a, b = net.station("N0CALL"), net.station("K1ABC-1")
        lk = a.connect("K1ABC-1")
        net.run(1)
        self.assertEqual(lk.state, CONNECTED)
        self.assertEqual(b.events, [("connect", "N0CALL", True)])
        self.assertEqual(a.events, [("connect", "K1ABC-1", False)])
        lk.write(b"hello\r")
        b.links["N0CALL"].write(b"hi there\r")
        net.run(2)
        self.assertEqual(bytes(b.data["N0CALL"]), b"hello\r")
        self.assertEqual(bytes(a.data["K1ABC-1"]), b"hi there\r")
        lk.disconnect()
        net.run(1)
        self.assertEqual(lk.state, DISCONNECTED)
        self.assertEqual(b.events[-1][0], "disconnect")
        self.assertEqual(a.events[-1], ("disconnect", "K1ABC-1", "disconnected"))

    def test_bulk_transfer_window_and_wraparound(self):
        net = Net()
        a, b = net.station("N0CALL", paclen=32), net.station("K1ABC")
        lk = a.connect("K1ABC")
        net.run(1)
        payload = bytes(random.Random(2).randrange(256) for _ in range(2000))      # 63 frames, many wraps
        lk.write(payload)
        net.run(60)
        self.assertEqual(bytes(b.data["N0CALL"]), payload)
        self.assertEqual(lk.outstanding, 0)

    def test_recovers_from_loss(self):
        for seed in range(5):
            net = Net(loss=0.2, seed=seed)
            a, b = net.station("N0CALL", paclen=40, retry=20), net.station("K1ABC", retry=20)
            lk = a.connect("K1ABC")
            net.run(20)
            self.assertEqual(lk.state, CONNECTED, f"seed {seed}")
            payload = bytes(range(256)) * 4
            lk.write(payload)
            net.run(300)
            self.assertEqual(bytes(b.data.get("N0CALL", b"")), payload, f"seed {seed}")
            self.assertGreater(lk.stats["i_resent"], 0)

    def test_busy_when_not_accepting(self):
        net = Net()
        a, b = net.station("N0CALL"), net.station("K1ABC")
        b.accept_incoming = lambda lk: False
        a.connect("K1ABC")
        net.run(1)
        self.assertEqual(a.events, [("disconnect", "K1ABC", "busy")])

    def test_retry_count_exceeded(self):
        net = Net()
        a = net.station("N0CALL", retry=3)
        a.connect("NOBODY")
        net.run(30)
        self.assertEqual(a.events, [("disconnect", "NOBODY", "retry count exceeded")])
        self.assertEqual(net.sent, 4)                         # SABM + 3 retries

    def test_link_failure_detected(self):
        net = Net()
        a, b = net.station("N0CALL", retry=3), net.station("K1ABC")
        lk = a.connect("K1ABC")
        net.run(1)
        net.mgrs.remove(b)                                    # peer goes off the air
        lk.write(b"anyone?")
        net.run(60)
        self.assertEqual(a.events[-1], ("disconnect", "K1ABC", "retry count exceeded"))

    def test_via_digipeater(self):
        net = Net(digi="RELAY")
        a, b = net.station("N0CALL"), net.station("K1ABC")
        lk = a.connect("K1ABC", via=["RELAY"])
        net.run(3)
        self.assertEqual(lk.state, CONNECTED)
        self.assertEqual([str(d) for d in b.links["N0CALL"].path], ["RELAY"])   # reply path reversed
        lk.write(b"via relay")
        net.run(3)
        self.assertEqual(bytes(b.data["N0CALL"]), b"via relay")

    def test_ignores_other_stations_and_ui(self):
        net = Net()
        a = net.station("N0CALL")
        self.assertFalse(a.receive(Frame.ui("K1ABC", "N0CALL", "ui")))
        self.assertFalse(a.receive(Frame.ui("K1ABC", "W1AW", "x")))
        self.assertEqual(a.links, {})


if __name__ == "__main__":
    unittest.main()
