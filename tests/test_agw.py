import select
import socket
import struct
import unittest

from hammodem.agw import HEADER, AgwServer, pack
from tests.test_tnc2 import Air, Port


class AgwPeer:
    """A minimal AGWPE client (as UI-View, Outpost, etc. would be)."""

    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port))
        self.s.setblocking(False)
        self.buf = bytearray()
        self.msgs = []

    def send(self, *a, **kw):
        self.s.sendall(pack(*a, **kw))

    def poll(self):
        try:
            while True:
                data = self.s.recv(65536)
                if not data:
                    break
                self.buf.extend(data)
        except BlockingIOError:
            pass
        while len(self.buf) >= HEADER.size:
            port, kind, pid, cf, ct, n, _ = HEADER.unpack_from(self.buf)
            if len(self.buf) < HEADER.size + n:
                break
            self.msgs.append((chr(kind), cf.split(b"\0")[0].decode(), ct.split(b"\0")[0].decode(),
                              bytes(self.buf[HEADER.size:HEADER.size + n])))
            del self.buf[:HEADER.size + n]

    def kinds(self, k):
        return [m for m in self.msgs if m[0] == k]


class AgwTest(unittest.TestCase):
    def setUp(self):
        self.air = Air(2)
        self.srv = AgwServer(self.air.tncs[0], port=0)
        self.client = AgwPeer(self.srv.srv.getsockname()[1])
        self.b = Port(self.air.tncs[1], "K1ABC")

    def tearDown(self):
        self.client.s.close()
        self.srv.close()

    def pump(self, seconds=0.0):
        for _ in range(max(1, int(seconds * 5))):
            for _ in range(3):
                r, w, _ = select.select(self.srv.fds(), self.srv.write_fds(), [], 0.01)
                self.srv.service(r, w)
            if seconds:
                self.air.run(0.2)
            self.client.poll()

    def test_version_ports_register(self):
        self.client.send("R")
        self.client.send("G")
        self.client.send("X", call_from="N0CALL")
        self.pump()
        self.assertEqual(struct.unpack("<hhhh", self.client.kinds("R")[0][3])[0], 2005)
        self.assertTrue(self.client.kinds("G")[0][3].startswith(b"1;Port1"))
        self.assertEqual(self.client.kinds("X")[0][3], b"\x01")

    def test_ui_out_and_monitor_in(self):
        self.client.send("m")
        self.client.send("M", b">from agw", pid=0xF0, call_from="N0CALL", call_to="APRS")
        self.pump(2)
        self.assertIn("N0CALL>APRS:>from agw", self.b.text())
        self.b.type("U CQ\rK\rhello agw\r")
        self.pump(2)
        mon = self.client.kinds("U")
        self.assertTrue(any(b"Fm K1ABC To CQ" in m[3] and b"hello agw" in m[3] for m in mon), mon)

    def test_connect_data_disconnect(self):
        self.client.send("X", call_from="N0CALL")
        self.client.send("C", call_from="N0CALL", call_to="K1ABC")
        self.pump(3)
        conn = self.client.kinds("C")
        self.assertTrue(conn and b"*** CONNECTED To Station K1ABC" in conn[0][3], conn)
        self.assertIn("*** CONNECTED to N0CALL", self.b.text())
        self.client.send("D", b"hello via AGW\r", pid=0xF0, call_from="N0CALL", call_to="K1ABC")
        self.b.type("hi AGW user\r")
        self.pump(4)
        self.assertIn("hello via AGW", self.b.text())
        self.assertIn(b"hi AGW user\r", b"".join(m[3] for m in self.client.kinds("D")))
        self.client.send("d", call_from="N0CALL", call_to="K1ABC")
        self.pump(3)
        self.assertTrue(self.client.kinds("d"))
        self.assertIn("*** DISCONNECTED", self.b.text())

    def test_incoming_connect_needs_registration(self):
        self.b.type("C N0CALL\r")
        self.pump(3)
        self.assertNotIn("CONNECTED", self.b.text())          # nobody owns N0CALL yet: no answer on the air
        self.client.send("X", call_from="N0CALL")
        self.pump(10)                                          # the caller's SABM retry now gets an answer
        self.assertIn("*** CONNECTED to N0CALL", self.b.text())
        self.assertTrue(any(b"CONNECTED With Station K1ABC" in m[3] for m in self.client.kinds("C")))


if __name__ == "__main__":
    unittest.main()
