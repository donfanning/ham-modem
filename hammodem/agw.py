"""AGWPE TCP/IP API server (the protocol of SV2AGW's Packet Engine, also offered by Direwolf on port 8000).

Every message is a 36-byte header plus data::

    port u8 + 3 pad | kind u8 + pad | pid u8 + pad | call_from 10s | call_to 10s | data_len u32le | user u32

Implemented kinds (client -> server): R version, G port list, g port capabilities, X/x register/unregister a
callsign, m toggle monitoring, k toggle raw frames, K raw frame out, M UI out, V UI via digipeaters, C/v connect
(direct / via), D connected data, d disconnect, Y/y outstanding frames, H heard list (empty).
Server -> client: R, G, g, X, C, D, d, Y, y, K (raw frames heard), U/I/S (monitor text).

Connected mode runs here (``ax25_link``), one ``LinkManager`` per callsign a client registered or connects from.
"""

from __future__ import annotations

import logging
import socket
import struct
import time

from .ax25 import Address, Ax25Error, Frame
from .ax25_link import CONNECTED, LinkManager, LinkParams

log = logging.getLogger("hammodem.agw")

HEADER = struct.Struct("<B3xBxBx10s10sII")


def pack(kind: str, data: bytes = b"", port: int = 0, pid: int = 0, call_from: str = "", call_to: str = "") -> bytes:
    return HEADER.pack(port, ord(kind), pid, call_from.encode()[:10], call_to.encode()[:10], len(data), 0) + data


def _call(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii", "replace").strip().upper()


def monitor_text(f: Frame, port: int = 0) -> tuple[str, str]:
    """AGW monitor kind and text, e.g. ``1:Fm N0CALL To APRS Via WIDE1-1 <UI pid=F0 Len=5 >[12:00:00]``."""
    via = f" Via {','.join(str(d) + ('*' if d.flag else '') for d in f.digis)}" if f.digis else ""
    k = f.kind
    if k == "UI":
        kind, detail = "U", f"<UI pid={f.pid:02X} Len={len(f.info)} >"
    elif k == "I":
        kind, detail = "I", f"<I S{f.ns} R{f.nr} pid={f.pid:02X} Len={len(f.info)} >"
    elif k in ("RR", "RNR", "REJ", "SREJ"):
        kind, detail = "S", f"<{k} R{f.nr} {'P' if f.command else 'F'}{'1' if f.poll else '0'} >"
    else:
        kind, detail = "S", f"<{k} >"
    text = f" {port + 1}:Fm {f.src} To {f.dest}{via} {detail}[{time.strftime('%H:%M:%S')}]\r"
    if f.info and kind in ("U", "I"):
        text += f.info.decode("latin-1") + "\r"
    return kind, text + "\0"


class AgwClient:
    def __init__(self, server: "AgwServer", sock: socket.socket, addr) -> None:
        self.server, self.sock, self.name = server, sock, f"agw:{addr[0]}:{addr[1]}"
        self.tnc = server.tnc
        self.rx = bytearray()
        self.pending = bytearray()
        self.monitor = False
        self.raw = False
        self.registered: set[str] = set()
        self.mgrs: dict[str, LinkManager] = {}

    # ---------------------------------------------------------------- endpoint interface (TNC side)

    def deliver(self, frame: bytes, port: int = 0) -> None:
        if self.raw:
            self.write(pack("K", bytes((port << 4,)) + frame, port))
        try:
            f = Frame.decode(frame)
        except Ax25Error:
            return
        for mgr in self.mgrs.values():
            mgr.receive(f)
        if self.monitor:
            kind, text = monitor_text(f, port)
            self.write(pack(kind, text.encode("latin-1", "replace"), port, f.pid or 0, str(f.src), str(f.dest)))

    def tick(self) -> None:
        for mgr in self.mgrs.values():
            mgr.tick()

    def reset(self) -> None:
        pass

    # ---------------------------------------------------------------- socket

    def write(self, data: bytes) -> None:
        if len(self.pending) > 1 << 20:
            return
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

    def feed(self, data: bytes) -> None:
        self.rx.extend(data)
        while len(self.rx) >= HEADER.size:
            port, kind, pid, cfrom, cto, dlen, _ = HEADER.unpack_from(self.rx)
            if dlen > 65536:
                log.warning("%s: oversized AGW message, dropping client data", self.name)
                self.rx.clear()
                return
            if len(self.rx) < HEADER.size + dlen:
                return
            data = bytes(self.rx[HEADER.size:HEADER.size + dlen])
            del self.rx[:HEADER.size + dlen]
            try:
                self._handle(chr(kind), port, pid, _call(cfrom), _call(cto), data)
            except (Ax25Error, ValueError) as exc:
                log.warning("%s: bad %r request: %s", self.name, chr(kind), exc)

    # ---------------------------------------------------------------- requests

    def _handle(self, kind: str, port: int, pid: int, cfrom: str, cto: str, data: bytes) -> None:
        tnc = self.tnc
        if kind == "R":
            self.write(pack("R", struct.pack("<hhhh", 2005, 0, 127, 0)))
        elif kind == "G":
            self.write(pack("G", f"1;Port1 ham-modem {self.server.description};".encode() + b"\0"))
        elif kind == "g":
            ch = tnc.channel
            caps = bytes((0, 0xFF, min(255, round(ch.txdelay * 100)), min(255, round(ch.txtail * 100)), ch.persist,
                          min(255, round(ch.slottime * 100)), ch.maxframe,
                          sum(len(m.links) for m in self.mgrs.values())))
            self.write(pack("g", caps + struct.pack("<I", 0), port))
        elif kind == "X":
            Address.parse(cfrom)
            self.registered.add(cfrom)
            self._mgr(cfrom)
            self.write(pack("X", b"\x01", port, call_from=cfrom))
        elif kind == "x":
            self.registered.discard(cfrom)
        elif kind == "m":
            self.monitor = not self.monitor
        elif kind == "k":
            self.raw = not self.raw
        elif kind == "K":
            if len(data) > 1:
                tnc.send(data[1:])
        elif kind in ("M", "V"):
            via: list[str] = []
            if kind == "V":
                n = data[0] if data else 0
                via = [_call(data[1 + 10 * i:11 + 10 * i]) for i in range(n)]
                data = data[1 + 10 * n:]
            f = Frame(Address.parse(cto), Address.parse(cfrom), [Address.parse(v) for v in via], 0x03, pid or 0xF0,
                      data)
            tnc.send(f.encode())
        elif kind in ("C", "v", "c"):
            via = []
            if kind == "v":
                n = data[0] if data else 0
                via = [_call(data[1 + 10 * i:11 + 10 * i]) for i in range(n)]
            mgr = self._mgr(cfrom)
            mgr.connect(cto, via)
        elif kind == "D":
            lk = self._mgr(cfrom).links.get(str(Address.parse(cto)))
            if lk is not None and lk.state == CONNECTED:
                lk.write(data)
        elif kind == "d":
            lk = self._mgr(cfrom).links.get(str(Address.parse(cto)))
            if lk is not None:
                lk.disconnect()
        elif kind == "Y":
            lk = self._mgr(cfrom).links.get(str(Address.parse(cto)))
            self.write(pack("Y", struct.pack("<I", lk.outstanding if lk else 0), port, call_from=cfrom, call_to=cto))
        elif kind == "y":
            self.write(pack("y", struct.pack("<I", len(tnc.channel.queue)), port))
        elif kind == "H":
            pass
        else:
            log.debug("%s: unsupported AGW kind %r", self.name, kind)

    def _mgr(self, call: str) -> LinkManager:
        key = str(Address.parse(call))
        mgr = self.mgrs.get(key)
        if mgr is None:
            mgr = self.mgrs[key] = LinkManager(key, self.tnc.send, LinkParams(), clock=self.tnc.clock)
            mycall = key
            mgr.accept_incoming = lambda lk, c=mycall: c in self.registered
            mgr.on_connect = lambda lk, incoming, c=mycall: self.write(pack(
                "C", f"*** CONNECTED {'With' if incoming else 'To'} Station {lk.name}\r".encode() + b"\0",
                call_from=lk.name, call_to=c))
            mgr.on_data = lambda lk, d, c=mycall: self.write(pack("D", d, pid=0xF0, call_from=lk.name, call_to=c))
            mgr.on_disconnect = lambda lk, why, c=mycall: self.write(pack(
                "d", (f"*** DISCONNECTED RETRYOUT With {lk.name}\r" if "retry" in why
                      else f"*** DISCONNECTED From Station {lk.name}\r").encode() + b"\0",
                call_from=lk.name, call_to=c))
        return mgr

    def close(self) -> None:
        for mgr in self.mgrs.values():
            for lk in mgr.links.values():
                lk.disconnect()
        self.sock.close()


class AgwServer:
    def __init__(self, tnc, host: str = "127.0.0.1", port: int = 8000, description: str = "") -> None:
        self.tnc, self.description = tnc, description
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((host, port))
        self.srv.listen(8)
        self.srv.setblocking(False)
        self.clients: dict[socket.socket, AgwClient] = {}
        self.name = f"agw://{host}:{self.srv.getsockname()[1]}"

    def fds(self):
        return [self.srv, *self.clients]

    def write_fds(self):
        return [s for s, c in self.clients.items() if c.pending]

    def service(self, readable, writable=()) -> None:
        for s in writable:
            if s in self.clients:
                self.clients[s].flush()
        if self.srv in readable:
            try:
                sock, addr = self.srv.accept()
            except OSError:
                sock = None
            if sock is not None:
                sock.setblocking(False)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                client = AgwClient(self, sock, addr)
                self.clients[sock] = client
                self.tnc.endpoints.append(client)
        for s in [s for s in readable if s in self.clients]:
            try:
                data = s.recv(65536)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                data = b""
            if data:
                self.clients[s].feed(data)
            else:
                self._drop(s)

    def _drop(self, s) -> None:
        client = self.clients.pop(s)
        self.tnc.detach(client)
        client.close()

    def close(self) -> None:
        for s in list(self.clients):
            self._drop(s)
        self.srv.close()
