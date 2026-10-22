import select
import socket
import unittest

from hammodem.ardop.arq import ArqStation
from hammodem.ardop.host import HostServer
from hammodem.ardop.tnc import ArdopTnc

FS = 8000
N = FS // 50


def free_port_pair() -> int:
    for _ in range(50):
        a = socket.socket()
        a.bind(("127.0.0.1", 0))
        port = a.getsockname()[1]
        b = socket.socket()
        try:
            b.bind(("127.0.0.1", port + 1))
            return port
        except OSError:
            continue
        finally:
            a.close()
            b.close()
    raise RuntimeError("no free port pair")


class Client:
    """A Winlink-client-like host: command lines on PORT, length-prefixed data on PORT+1."""

    def __init__(self, port):
        self.c = socket.create_connection(("127.0.0.1", port))
        self.d = socket.create_connection(("127.0.0.1", port + 1))
        for s in (self.c, self.d):
            s.setblocking(False)
        self.lines, self.data, self._cb, self._db = [], {}, b"", b""

    def cmd(self, text):
        self.c.sendall(text.encode() + b"\r")

    def send(self, data):
        self.d.sendall(len(data).to_bytes(2, "big") + data)

    def poll(self):
        for s in (self.c, self.d):
            try:
                chunk = s.recv(65536)
            except BlockingIOError:
                continue
            if s is self.c:
                self._cb += chunk
                while b"\r" in self._cb:
                    line, self._cb = self._cb.split(b"\r", 1)
                    self.lines.append(line.decode())
            else:
                self._db += chunk
                while len(self._db) >= 2 and len(self._db) >= 2 + int.from_bytes(self._db[:2], "big"):
                    n = int.from_bytes(self._db[:2], "big")
                    frame, self._db = self._db[2:2 + n], self._db[2 + n:]
                    self.data.setdefault(frame[:3].decode(), bytearray()).extend(frame[3:])

    def close(self):
        self.c.close()
        self.d.close()


class Bench:
    """Two TNCs back to back, each behind its own host interface, clocked by the test."""

    def __init__(self):
        self.tncs, self.servers, self.clients = [], [], []
        for call in ("N0CALL", "K1ABC"):
            st = ArqStation(call, fs=FS, seed=len(call))
            port = free_port_pair()
            self.tncs.append(ArdopTnc(st))
            self.servers.append(HostServer(st, "127.0.0.1", port))
            self.clients.append(Client(port))
        self.a, self.b = self.clients
        self._out = [None, None]
        self.step(5)

    def step(self, ticks=1):
        for _ in range(ticks):
            fds = [f for s in self.servers for f in s.fds()]
            wfds = [f for s in self.servers for f in s.write_fds()]
            r, w, _ = select.select(fds, wfds, [], 0)
            for s in self.servers:
                s.service(r, w)
            outs = [t.audio_out(N) for t in self.tncs]
            self.tncs[0].audio_in(outs[1])
            self.tncs[1].audio_in(outs[0])
            for c in self.clients:
                c.poll()

    def until(self, cond, seconds):
        for _ in range(int(seconds * 50)):
            self.step()
            if cond():
                return True
        return False

    def close(self):
        for c in self.clients:
            c.close()
        for s in self.servers:
            s.close()


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.bench = Bench()

    def tearDown(self):
        self.bench.close()

    def ask(self, text):
        a = self.bench.a
        before = len(a.lines)
        a.cmd(text)
        self.bench.step(2)
        return a.lines[before:]

    def test_replies_follow_ardopcf(self):
        self.assertEqual(self.ask("mycall N0CALL-1"), ["MYCALL now N0CALL-1"])
        self.assertEqual(self.ask("MYCALL"), ["MYCALL N0CALL-1"])
        self.assertEqual(self.ask("ARQBW 2000MAX"), ["ARQBW now 2000MAX"])
        self.assertEqual(self.ask("ARQBW 3000MAX"), ["FAULT Syntax Err: ARQBW 3000MAX"])
        self.assertEqual(self.ask("LISTEN FALSE"), ["LISTEN now FALSE"])
        self.assertEqual(self.ask("STATE"), ["STATE DISC"])
        self.assertEqual(self.ask("BUFFER"), ["BUFFER 0"])
        self.assertEqual(self.ask("CWID"), ["CWID FALSE"])
        self.assertTrue(self.ask("VERSION")[0].startswith("VERSION "))
        self.assertEqual(self.ask("FOO"), ["FAULT CMD FOO not recoginized"])
        self.assertEqual(self.ask("DISCONNECT"), ["DISCONNECT IGNORED"])


class SessionOverHostTest(unittest.TestCase):
    def test_client_drives_a_connected_session(self):
        bench = Bench()
        a, b = bench.a, bench.b
        try:
            for c, call in ((a, "N0CALL"), (b, "K1ABC")):
                for text in ("INITIALIZE", f"MYCALL {call}", "PROTOCOLMODE ARQ", "ARQBW 500MAX", "LISTEN TRUE"):
                    c.cmd(text)
            bench.step(5)
            a.send(b"Hello Winlink " * 30)
            bench.step(2)
            self.assertIn("BUFFER 420", a.lines)
            a.cmd("ARQCALL K1ABC 5")
            self.assertTrue(bench.until(lambda: len(b.data.get("ARQ", b"")) >= 420, 60))
            self.assertEqual(bytes(b.data["ARQ"]), b"Hello Winlink " * 30)
            self.assertIn("CONNECTED N0CALL 500", b.lines)
            self.assertIn("CONNECTED K1ABC 500", a.lines)
            b.send(b"FC EM 12345 ok")
            self.assertTrue(bench.until(lambda: bytes(a.data.get("ARQ", b"")) == b"FC EM 12345 ok", 40))
            a.cmd("DISCONNECT")
            self.assertTrue(bench.until(lambda: "DISCONNECTED" in a.lines and "DISCONNECTED" in b.lines, 30))
            self.assertTrue(bench.until(lambda: "IDF" in b.data or "IDF" in a.data, 10))
        finally:
            bench.close()


if __name__ == "__main__":
    unittest.main()
