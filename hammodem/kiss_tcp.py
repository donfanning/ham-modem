"""KISS over TCP (Direwolf's port 8001 convention): any number of clients, each its own KISS endpoint."""

from __future__ import annotations

import socket


class _Conn:
    def __init__(self, sock: socket.socket, addr) -> None:
        self.sock, self.name = sock, f"tcp:{addr[0]}:{addr[1]}"
        self.pending = bytearray()
        self.endpoint = None

    def write(self, data: bytes) -> None:
        if len(self.pending) > 1 << 20:
            return                              # client stopped reading; drop rather than grow without bound
        self.pending.extend(data)
        self.flush()

    def flush(self) -> None:
        try:
            n = self.sock.send(self.pending)
            del self.pending[:n]
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            self.pending.clear()


class KissTcpServer:
    def __init__(self, tnc, host: str = "127.0.0.1", port: int = 8001) -> None:
        self.tnc = tnc
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((host, port))
        self.srv.listen(8)
        self.srv.setblocking(False)
        self.conns: dict[socket.socket, _Conn] = {}
        self.name = f"kiss-tcp://{host}:{self.srv.getsockname()[1]}"

    def fds(self):
        return [self.srv, *self.conns]

    def write_fds(self):
        return [s for s, c in self.conns.items() if c.pending]

    def service(self, readable, writable=()) -> None:
        for s in writable:
            if s in self.conns:
                self.conns[s].flush()
        if self.srv in readable:
            try:
                sock, addr = self.srv.accept()
            except OSError:
                sock = None
            if sock is not None:
                sock.setblocking(False)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn = _Conn(sock, addr)
                conn.endpoint = self.tnc.attach(conn.write, conn.name)
                self.conns[sock] = conn
        for s in [s for s in readable if s in self.conns]:
            conn = self.conns[s]
            try:
                data = s.recv(4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                data = b""
            if data:
                conn.endpoint.feed(data)
            else:
                self._drop(s)

    def _drop(self, s) -> None:
        conn = self.conns.pop(s)
        self.tnc.detach(conn.endpoint)
        s.close()

    def close(self) -> None:
        for s in list(self.conns):
            self._drop(s)
        self.srv.close()
