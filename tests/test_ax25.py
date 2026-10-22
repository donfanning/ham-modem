import unittest

from hammodem import ax25
from hammodem.ax25 import Address, Ax25Error, Frame


class Ax25Test(unittest.TestCase):
    def test_address_parse(self):
        self.assertEqual(Address.parse("n0call-7"), Address("N0CALL", 7, False))
        self.assertEqual(Address.parse("WIDE1-1*"), Address("WIDE1", 1, True))
        for bad in ("TOOLONGCALL", "N0CALL-16", "N0-CALL", ""):
            with self.assertRaises(Ax25Error):
                Address.parse(bad)

    def test_address_encoding(self):
        raw = Address("APRS").encode(False)
        self.assertEqual(raw, bytes((0x82, 0xA0, 0xA4, 0xA6, 0x40, 0x40, 0x60)))

    def test_ui_round_trip(self):
        f = Frame.ui("N0CALL-9", "APRS", "!4903.50N/07201.75W-hello", "WIDE1-1,WIDE2-2")
        raw = f.encode()
        g = Frame.decode(raw)
        self.assertEqual(g.kind, "UI")
        self.assertEqual(g.encode(), raw)
        self.assertEqual(g.monitor(), "N0CALL-9>APRS,WIDE1-1,WIDE2-2:!4903.50N/07201.75W-hello")
        self.assertTrue(g.command)

    def test_repeated_digi_marked(self):
        f = Frame.ui("N0CALL", "CQ", "x", "RELAY*,WIDE")
        self.assertEqual(Frame.decode(f.encode()).path_text(), "N0CALL>CQ,RELAY*,WIDE")

    def test_connected_mode_kinds(self):
        sabm = Frame(Address("K1ABC"), Address("N0CALL"), control=ax25.SABM | ax25.PF, pid=None)
        g = Frame.decode(sabm.encode())
        self.assertEqual((g.kind, g.poll, g.has_pid), ("SABM", True, False))
        self.assertEqual(g.monitor(), "N0CALL>K1ABC <SABM P>")
        i = Frame(Address("K1ABC"), Address("N0CALL"), control=(3 << 5) | (2 << 1), info=b"hi")
        g = Frame.decode(i.encode())
        self.assertEqual((g.kind, g.nr, g.ns, g.pid, g.info), ("I", 3, 2, ax25.PID_NO_L3, b"hi"))
        rr = Frame(Address("N0CALL"), Address("K1ABC"), control=ax25.RR | (5 << 5) | ax25.PF, pid=None,
                   command=False)
        g = Frame.decode(rr.encode())
        self.assertEqual((g.kind, g.nr, g.command), ("RR", 5, False))
        self.assertEqual(g.monitor(), "K1ABC>N0CALL <RR R5 F>")

    def test_truncated(self):
        with self.assertRaises(Ax25Error):
            Frame.decode(Address("APRS").encode(False) + b"\x00" * 3)


if __name__ == "__main__":
    unittest.main()
