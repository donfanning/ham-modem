import random
import unittest

import numpy as np

from hammodem import kiss
from hammodem.ax25 import Frame
from hammodem.tnc import Tnc

N = 160


class SimClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Port:
    def __init__(self, tnc, mycall):
        self.out = bytearray()
        self.term = tnc.attach_tnc2(self.out.extend, mycall, mycall)

    def type(self, text):
        self.term.feed(text.encode() if isinstance(text, str) else text)

    def text(self):
        return self.out.decode("latin-1")


class Air:
    """Stations on a ConfBridge-like channel (each hears the others, never itself), in simulated time."""

    def __init__(self, n):
        self.clock = SimClock()
        self.tncs = [Tnc("afsk1200", 8000, clock=self.clock, rng=random.Random(i), txdelay=0.1, slottime=0.05)
                     for i in range(n)]

    def run(self, seconds):
        for _ in range(int(seconds * 50)):
            for t in self.tncs:
                t.tick()
            out = [t.audio_out(N).astype(np.int32) for t in self.tncs]
            total = sum(out)
            for t, own in zip(self.tncs, out):
                t.audio_in(np.clip(total - own, -32768, 32767).astype(np.int16))
            self.clock.t += 0.02


class Tnc2CommandTest(unittest.TestCase):
    def setUp(self):
        self.air = Air(1)
        self.p = Port(self.air.tncs[0], "N0CALL")
        self.p.out.clear()

    def test_mycall_abbreviation_and_display(self):
        self.p.type("my k1abc-2\r")
        self.assertIn("MYCALL was N0CALL", self.p.text())
        self.p.type("MYCALL\r")
        self.assertIn("MYCALL K1ABC-2", self.p.text())
        self.p.type("disp\r")
        self.assertIn("PACLEN", self.p.text())

    def test_bad_commands(self):
        self.p.type("XYZZY\r")
        self.assertIn("?EH", self.p.text())
        self.p.type("PACLEN 999\r")
        self.assertIn("?bad parameter", self.p.text())
        self.p.type("MONITOR MAYBE\r")
        self.assertIn("?bad parameter", self.p.text())

    def test_numbers_set_channel(self):
        self.p.type("TXDELAY 25\rPERSIST 255\r")
        ch = self.air.tncs[0].channel
        self.assertEqual((round(ch.txdelay * 100), ch.persist), (25, 255))

    def test_backspace_and_echo(self):
        self.p.type("MYCALLX\x08 W1AW\r")
        self.assertIn("MYCALL was N0CALL", self.p.text())
        self.p.type("ECHO OFF\r")
        self.p.out.clear()
        self.p.type("MY\r")
        self.assertTrue(self.p.text().startswith("MYCALL W1AW"))

    def test_kiss_on_and_return(self):
        self.p.type("KISS ON\r")
        self.p.out.clear()
        self.p.type(kiss.encode(bytes((40,)), command=kiss.TXDELAY))
        self.assertAlmostEqual(self.air.tncs[0].channel.txdelay, 0.4)
        self.p.type(bytes((0xC0, 0xFF, 0xC0)))
        self.assertIn("cmd:", self.p.text())


class Tnc2LinkTest(unittest.TestCase):
    def test_connect_chat_disconnect_over_audio(self):
        air = Air(2)
        a, b = Port(air.tncs[0], "N0CALL"), Port(air.tncs[1], "K1ABC")
        a.type("C K1ABC\r")
        air.run(3)
        self.assertIn("*** CONNECTED to K1ABC", a.text())
        self.assertIn("*** CONNECTED to N0CALL", b.text())
        a.type("Hello from N0CALL\r")
        b.type("Hi, K1ABC here\r")
        air.run(4)
        self.assertIn("Hello from N0CALL\r\n", b.text())
        self.assertIn("Hi, K1ABC here\r\n", a.text())
        a.type("\x03D\r")
        air.run(3)
        self.assertIn("*** DISCONNECTED", a.text())
        self.assertIn("*** DISCONNECTED", b.text())
        self.assertTrue(a.text().endswith("cmd:"))

    def test_unproto_converse_and_monitor(self):
        air = Air(2)
        a, b = Port(air.tncs[0], "N0CALL"), Port(air.tncs[1], "K1ABC")
        a.type("U APRS VIA WIDE1-1\rK\r>status text\r")
        air.run(2)
        self.assertIn("N0CALL>APRS,WIDE1-1:>status text", b.text())

    def test_conok_off_gives_busy(self):
        air = Air(2)
        a, b = Port(air.tncs[0], "N0CALL"), Port(air.tncs[1], "K1ABC")
        b.type("CONOK OFF\r")
        a.type("C K1ABC\r")
        air.run(3)
        self.assertIn("*** K1ABC busy", a.text())

    def test_kiss_client_sees_connected_traffic_monitor(self):
        air = Air(2)
        a, b = Port(air.tncs[0], "N0CALL"), Port(air.tncs[1], "K1ABC")
        wire = bytearray()
        air.tncs[1].attach(wire.extend, "kiss")
        a.type("C K1ABC\r")
        air.run(3)
        kinds = [Frame.decode(d).kind for _, _, d in kiss.Decoder().feed(bytes(wire))]
        self.assertIn("SABM", kinds)


if __name__ == "__main__":
    unittest.main()
