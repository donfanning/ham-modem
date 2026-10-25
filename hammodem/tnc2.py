"""TNC-2 style command interpreter: what a dumb terminal or old packet BBS/terminal program expects.

A serial-port user gets the TAPR TNC-2 experience: a ``cmd:`` prompt, ``MYCALL``, ``CONNECT``, multiple AX.25
streams, converse and transparent modes, monitoring, ``UNPROTO``/``BEACON``, and ``KISS ON`` to hand the port over
to a KISS host. ``STREAM n`` selects a zero-based stream; Ctrl-\\ cycles among active streams in converse mode.
Commands may be abbreviated as on the TNC-2 (``C`` for CONNECT, ``K`` for CONVERSE, ``MY`` for MYCALL ...).
Ctrl-C returns from converse/transparent mode to command mode.

Settings can be persisted to a JSON file (NVRAM): ``load_nvram(path)`` on startup and ``WRITE`` from the
``cmd:`` prompt to save.  ``--nvram PATH`` in main.py wires this up automatically.
"""

from __future__ import annotations

import json
import logging
import os
import time

from . import kiss
from .ax25 import Address, Ax25Error, Frame
from .ax25_link import CONNECTED, DISCONNECTED, Link, LinkManager, LinkParams

log = logging.getLogger("hammodem.tnc2")

CTRL_C = 0x03
CTRL_STREAMSW = 0x1C
BANNER = b"\r\nham-modem TNC-2 emulation\r\n"

COMMAND, CONVERSE, TRANSPARENT, KISS_MODE = "command", "converse", "transparent", "kiss"

# name, shortest accepted abbreviation
_COMMANDS = [
    ("BEACON", 1), ("BTEXT", 2), ("CONNECT", 1), ("CONOK", 4), ("CONVERSE", 4), ("DISCONNE", 1), ("DISPLAY", 4),
    ("ECHO", 1), ("FRACK", 1), ("HELP", 1), ("K", 1), ("KISS", 4), ("MAXFRAME", 3), ("MAXSTREAM", 4),
    ("MCON", 3), ("MHEARD", 2),
    ("MONITOR", 1), ("MYCALL", 2), ("PACLEN", 1), ("PERSIST", 4), ("RESTART", 7), ("RETRY", 2), ("SLOTTIME", 2),
    ("STREAM", 3), ("TRANS", 1), ("TXDELAY", 2), ("UNPROTO", 1), ("WRITE", 2), ("?", 1),
]


def _lookup(word: str) -> str | None:
    word = word.upper()
    exact = [n for n, _ in _COMMANDS if n == word]
    if exact:
        return exact[0]
    hits = [n for n, m in _COMMANDS if n.startswith(word) and len(word) >= m]
    return hits[0] if len(hits) == 1 else None


def _onoff(arg: str) -> bool | None:
    a = arg.upper()
    return True if a in ("ON", "YES", "1") else False if a in ("OFF", "NO", "0") else None


class Tnc2Terminal:
    """One serial port in TNC-2 mode. ``write`` sends bytes to the terminal; ``feed`` takes its keystrokes."""

    def __init__(self, tnc, write, name: str = "", mycall: str = "NOCALL") -> None:
        self.tnc, self._write, self.name = tnc, write, name
        self.mode = COMMAND
        self.line = bytearray()
        self.echo = True
        self.monitor = True
        self.mcon = False
        self.conok = True
        self.unproto = (Address("CQ"), [])
        self.btext = ""
        self.beacon_every = 0.0                 # seconds, 0 = off
        self._next_beacon = 0.0
        self.heard: dict[str, tuple[float, int]] = {}
        self.params = LinkParams()
        self.mgr = LinkManager(mycall, tnc.send, self.params, clock=tnc.clock)
        self.mgr.on_connect = self._on_connect
        self.mgr.on_data = self._on_data
        self.mgr.on_disconnect = self._on_disconnect
        self.mgr.accept_incoming = self._accept_incoming
        self.max_streams = 4
        self.current_stream = 0
        self.streams: dict[int, Link] = {}
        self._awaiting_link = False
        self._kiss: kiss.Decoder | None = None
        self._nvram_path: str | None = None
        self._out(BANNER)
        self._prompt()

    @property
    def link(self) -> Link | None:
        """The link selected by the terminal, retained as a compatibility shorthand."""
        return self.streams.get(self.current_stream)

    def _stream_for_link(self, link: Link) -> int | None:
        return next((n for n, lk in self.streams.items() if lk is link), None)

    def _free_stream(self) -> int | None:
        return next((n for n in range(self.max_streams) if n not in self.streams), None)

    def _accept_incoming(self, link: Link) -> bool:
        return self.conok and (self._stream_for_link(link) is not None or self._free_stream() is not None)

    def _stream_status(self, announce: bool = True) -> None:
        link = self.link
        peer = f"{link.name} {link.state.upper()}" if link else "DISCONNECTED"
        if announce:
            self._say(f"STREAM {self.current_stream}: {peer}")

    def _next_stream(self, announce: bool = True) -> None:
        active = sorted(n for n, link in self.streams.items() if link.state != DISCONNECTED)
        if self.current_stream in active and len(active) > 1:
            i = active.index(self.current_stream)
            self.current_stream = active[(i + 1) % len(active)]
        else:
            self.current_stream = (self.current_stream + 1) % self.max_streams
        self._stream_status(announce)

    # ---------------------------------------------------------------- output helpers

    def _out(self, data: bytes) -> None:
        self._write(data)

    def _say(self, text: str) -> None:
        self._out(b"\r\n" + text.encode("latin-1") + b"\r\n")

    def _prompt(self) -> None:
        self._out(b"cmd:")

    # ---------------------------------------------------------------- frames heard / ticks (from the TNC)

    def deliver(self, frame: bytes, port: int = 0) -> None:
        if self.mode == KISS_MODE:
            self._out(kiss.encode(frame, port))
            return
        try:
            f = Frame.decode(frame)
        except Ax25Error:
            return
        self.mgr.receive(f)
        self.heard[str(f.src)] = (time.time(), self.heard.get(str(f.src), (0, 0))[1] + 1)
        mine = (f.dest.call, f.dest.ssid) == (self.mgr.mycall.call, self.mgr.mycall.ssid) and f.kind != "UI"
        if not self.monitor or mine:
            return
        if self.mode == COMMAND or (self.mcon and self.mode == CONVERSE):
            self._out(f.monitor().encode("latin-1", "replace") + b"\r\n")

    def reset(self) -> None:
        """The port was closed (DTR drop): leave KISS, drop the connection quietly."""
        for link in list(self.streams.values()):
            link.disconnect()
        self.mode, self._kiss = COMMAND, None
        self.line.clear()

    def tick(self) -> None:
        self.mgr.tick()
        now = self.tnc.clock()
        if self.beacon_every and self.btext and now >= self._next_beacon:
            self._next_beacon = now + self.beacon_every
            self._send_ui(self.btext.encode("latin-1"), Address("BEACON"))

    # ---------------------------------------------------------------- input

    def feed(self, data: bytes) -> None:
        if self.mode == KISS_MODE:
            for port, command, payload in self._kiss.feed(data):
                if command == kiss.RETURN:
                    self.mode, self._kiss = COMMAND, None
                    self._say("KISS OFF")
                    self._prompt()
                else:
                    self.tnc.kiss_command(self, port, command, payload)
            return
        if self.mode == TRANSPARENT:
            if CTRL_C in data:
                head, _, tail = data.partition(bytes((CTRL_C,)))
                self._send_data(head)
                self._to_command()
                self.feed(tail)
            else:
                self._send_data(data)
            return
        for b in data:
            if b == CTRL_C:
                self.line.clear()
                if self.mode != COMMAND:
                    self._to_command()
                else:
                    self._out(b"\r\n")
                    self._prompt()
            elif b == CTRL_STREAMSW and self.mode == CONVERSE:
                if self.line:
                    self._send_data(bytes(self.line))
                    self.line.clear()
                self._next_stream()
            elif b in (0x08, 0x7F):
                if self.line:
                    self.line.pop()
                    if self.echo:
                        self._out(b"\x08 \x08")
            elif b == 0x0D:
                line, self.line = bytes(self.line), bytearray()
                if self.echo:
                    self._out(b"\r\n")
                if self.mode == COMMAND:
                    self._awaiting_link = False
                    self._command(line.decode("latin-1").strip())
                    if self.mode == COMMAND and not self._awaiting_link:
                        self._prompt()          # CONNECT/DISCONNE prompt again once the link settles
                else:
                    self._send_data(line + b"\r")
            elif b == 0x0A:
                continue
            else:
                self.line.append(b)
                if self.echo:
                    self._out(bytes((b,)))

    def _to_command(self) -> None:
        self.mode = COMMAND
        self._out(b"\r\n")
        self._prompt()

    def _send_data(self, data: bytes) -> None:
        if not data:
            return
        if self.link is not None and self.link.state == CONNECTED:
            self.link.write(data)
        elif self.link is None:
            self._send_ui(data, *self.unproto)

    def _send_ui(self, info: bytes, dest: Address, via: list[Address] | None = None) -> None:
        self.tnc.send(Frame(dest, self.mgr.mycall, [Address(d.call, d.ssid) for d in via or []], 0x03, 0xF0,
                            info).encode())

    # ---------------------------------------------------------------- link events

    def _on_connect(self, link, incoming: bool) -> None:
        current = self.link
        stream = self._stream_for_link(link)
        if stream is None:
            stream = self._free_stream()
            if stream is None:
                link.disconnect()
                return
            self.streams[stream] = link
        if not incoming or current is None or current is link or current.state == DISCONNECTED:
            self.current_stream = stream
            self.mode = CONVERSE
            self.line.clear()
        self._say(f"*** CONNECTED to {link.name} on stream {stream}")

    def _on_data(self, link, data: bytes) -> None:
        if link is self.link:
            self._out(data.replace(b"\r", b"\r\n") if self.mode != TRANSPARENT else data)
        elif self.mcon:
            stream = self._stream_for_link(link)
            self._out(f"[STREAM {stream}] ".encode() + data.replace(b"\r", b"\r\n"))

    def _on_disconnect(self, link, reason: str) -> None:
        stream = self._stream_for_link(link)
        if stream is None:
            return
        del self.streams[stream]
        text = {"busy": f"*** {link.name} busy", "retry count exceeded": "*** retry count exceeded"}.get(reason)
        if stream == self.current_stream:
            if text:
                self._say(text)
            self._say("*** DISCONNECTED")
        else:
            self._say(text or f"*** {link.name} DISCONNECTED (stream {stream})")
        if stream == self.current_stream and self.mode != KISS_MODE:
            self.mode = COMMAND
            self.line.clear()
            self._prompt()

    # ---------------------------------------------------------------- commands

    def _command(self, text: str) -> None:
        if not text:
            return
        word, _, arg = text.partition(" ")
        arg = arg.strip()
        name = _lookup(word)
        if name is None:
            self._out(b"?EH\r\n")
            return
        try:
            handler = getattr(self, "_cmd_" + {"?": "HELP", "K": "CONVERSE"}.get(name, name).lower())
            handler(arg)
        except (ValueError, Ax25Error) as exc:
            self._out(f"?bad parameter ({exc})\r\n".encode() if str(exc) else b"?bad parameter\r\n")

    def _show(self, name: str, value) -> None:
        if isinstance(value, bool):
            value = "ON" if value else "OFF"
        self._out(f"{name} {value}\r\n".encode("latin-1"))

    def _flag(self, attr: str, name: str, arg: str) -> None:
        if not arg:
            self._show(name, getattr(self, attr))
            return
        v = _onoff(arg)
        if v is None:
            raise ValueError("ON or OFF")
        setattr(self, attr, v)
        self._show(f"{name} was", not v)

    def _number(self, name: str, arg: str, get, put, lo: int, hi: int) -> None:
        if not arg:
            self._show(name, get())
            return
        v = int(arg)
        if not lo <= v <= hi:
            raise ValueError(f"{lo}-{hi}")
        old = get()
        put(v)
        self._show(f"{name} was", old)

    def _cmd_mycall(self, arg: str) -> None:
        if not arg:
            self._show("MYCALL", self.mgr.mycall)
            return
        if self.streams:
            raise ValueError("not while connected")
        old, self.mgr.mycall = self.mgr.mycall, Address.parse(arg)
        self.mgr.links.clear()
        self._show("MYCALL was", old)

    def _cmd_connect(self, arg: str) -> None:
        if not arg:
            self._out(f"Link state is: {self.link.state.upper() if self.link else 'DISCONNECTED'}\r\n".encode())
            return
        if self.link is not None:
            raise ValueError("already connected")
        if self.current_stream >= self.max_streams:
            raise ValueError("select an enabled stream")
        if self.mgr.mycall.call == "NOCALL":
            self._out(b"?MYCALL not set\r\n")
            return
        call, via = self._parse_path(arg)
        link = self.mgr.link(str(call), [str(v) for v in via])
        self.streams[self.current_stream] = link
        link.connect()
        self._awaiting_link = True

    def _cmd_disconne(self, arg: str) -> None:
        if self.link is None:
            self._out(b"*** not connected\r\n")
            return
        self.link.disconnect()
        self._awaiting_link = True

    def _cmd_converse(self, arg: str) -> None:
        self.mode = CONVERSE

    def _cmd_trans(self, arg: str) -> None:
        self.mode = TRANSPARENT

    def _cmd_monitor(self, arg: str) -> None:
        self._flag("monitor", "MONITOR", arg)

    def _cmd_mcon(self, arg: str) -> None:
        self._flag("mcon", "MCON", arg)

    def _cmd_echo(self, arg: str) -> None:
        self._flag("echo", "ECHO", arg)

    def _cmd_conok(self, arg: str) -> None:
        self._flag("conok", "CONOK", arg)

    def _cmd_unproto(self, arg: str) -> None:
        if not arg:
            dest, via = self.unproto
            self._show("UNPROTO", str(dest) + (" VIA " + ",".join(map(str, via)) if via else ""))
            return
        self.unproto = self._parse_path(arg)

    def _cmd_btext(self, arg: str) -> None:
        if not arg:
            self._show("BTEXT", self.btext)
            return
        self.btext = arg[:120]

    def _cmd_beacon(self, arg: str) -> None:
        if not arg:
            self._show("BEACON", f"EVERY {int(self.beacon_every // 10)}")
            return
        parts = arg.upper().split()
        n = int(parts[-1])
        if not 0 <= n <= 250:
            raise ValueError("0-250")
        self.beacon_every = n * 10.0                         # TNC-2 units: 10 s
        self._next_beacon = self.tnc.clock()

    def _cmd_txdelay(self, arg: str) -> None:
        ch = self.tnc.channel
        self._number("TXDELAY", arg, lambda: round(ch.txdelay * 100), lambda v: setattr(ch, "txdelay", v / 100), 0, 120)

    def _cmd_persist(self, arg: str) -> None:
        ch = self.tnc.channel
        self._number("PERSIST", arg, lambda: ch.persist, lambda v: setattr(ch, "persist", v), 0, 255)

    def _cmd_slottime(self, arg: str) -> None:
        ch = self.tnc.channel
        self._number("SLOTTIME", arg, lambda: round(ch.slottime * 100), lambda v: setattr(ch, "slottime", v / 100),
                     0, 250)

    def _cmd_frack(self, arg: str) -> None:
        p = self.params
        self._number("FRACK", arg, lambda: int(p.frack), lambda v: setattr(p, "frack", float(v)), 1, 15)

    def _cmd_maxframe(self, arg: str) -> None:
        p = self.params
        self._number("MAXFRAME", arg, lambda: p.maxframe, lambda v: setattr(p, "maxframe", v), 1, 7)

    def _cmd_paclen(self, arg: str) -> None:
        p = self.params
        self._number("PACLEN", arg, lambda: p.paclen, lambda v: setattr(p, "paclen", v), 1, 256)

    def _cmd_retry(self, arg: str) -> None:
        p = self.params
        self._number("RETRY", arg, lambda: p.retry, lambda v: setattr(p, "retry", v), 0, 15)

    def _cmd_kiss(self, arg: str) -> None:
        if _onoff(arg) is not True:
            raise ValueError("KISS ON")
        for link in list(self.streams.values()):
            link.disconnect()
        self._say("KISS ON (send FEND FF FEND to return)")
        self.mode, self._kiss = KISS_MODE, kiss.Decoder()

    def _cmd_mheard(self, arg: str) -> None:
        for call, (when, n) in sorted(self.heard.items(), key=lambda kv: -kv[1][0]):
            self._out(f"{call:<10} {time.strftime('%H:%M:%S', time.localtime(when))}  {n} frames\r\n".encode())

    def _cmd_display(self, arg: str) -> None:
        ch, p = self.tnc.channel, self.params
        for name, value in [("MYCALL", self.mgr.mycall), ("MONITOR", self.monitor), ("MCON", self.mcon),
                            ("ECHO", self.echo), ("CONOK", self.conok), ("UNPROTO", self.unproto[0]),
                            ("TXDELAY", round(ch.txdelay * 100)), ("PERSIST", ch.persist),
                            ("SLOTTIME", round(ch.slottime * 100)), ("FRACK", int(p.frack)), ("MAXFRAME", p.maxframe),
                            ("PACLEN", p.paclen), ("RETRY", p.retry), ("BTEXT", self.btext),
                            ("BEACON", f"EVERY {int(self.beacon_every // 10)}"),
                            ("MAXSTREAM", self.max_streams), ("STREAM", self.current_stream)]:
            self._show(f"  {name:<9}", value)

    def _cmd_stream(self, arg: str) -> None:
        if not arg:
            for stream in range(self.max_streams):
                link = self.streams.get(stream)
                state = f"{link.name} {link.state.upper()}" if link else "DISCONNECTED"
                self._out(f"STREAM {stream}: {state}\r\n".encode("latin-1"))
            return
        stream = int(arg)
        if not 0 <= stream < self.max_streams:
            raise ValueError(f"0-{self.max_streams - 1}")
        self.current_stream = stream
        self._stream_status()

    def _cmd_maxstream(self, arg: str) -> None:
        if not arg:
            self._show("MAXSTREAM", self.max_streams)
            return
        n = int(arg)
        if not 1 <= n <= 10:
            raise ValueError("1-10")
        if any(stream >= n for stream in self.streams):
            raise ValueError("disconnect streams above new maximum first")
        old, self.max_streams = self.max_streams, n
        self._show("MAXSTREAM was", old)
        if self.current_stream >= n:
            self.current_stream = 0

    def _cmd_write(self, arg: str) -> None:
        if not self._nvram_path:
            self._say("no NVRAM path configured (use --nvram)")
            return
        self.save_nvram(self._nvram_path)
        self._say(f"settings saved to {self._nvram_path}")

    def _cmd_restart(self, arg: str) -> None:
        for link in list(self.streams.values()):
            link.disconnect()
        self._out(BANNER)

    def _cmd_help(self, arg: str) -> None:
        self._out(b"Commands: " + " ".join(n for n, _ in _COMMANDS if len(n) > 1).encode() + b"\r\n")

    # ---------------------------------------------------------------- NVRAM persistence

    def save_nvram(self, path: str) -> None:
        """Save current settings to a JSON file."""
        ch, p = self.tnc.channel, self.params
        dest, via = self.unproto
        data = {
            "mycall": str(self.mgr.mycall),
            "monitor": self.monitor,
            "mcon": self.mcon,
            "echo": self.echo,
            "conok": self.conok,
            "unproto": str(dest) + (" VIA " + ",".join(map(str, via)) if via else ""),
            "btext": self.btext,
            "beacon_every": int(self.beacon_every // 10),
            "txdelay": round(ch.txdelay * 100),
            "persist": ch.persist,
            "slottime": round(ch.slottime * 100),
            "frack": int(p.frack),
            "maxframe": p.maxframe,
            "paclen": p.paclen,
            "retry": p.retry,
            "max_streams": self.max_streams,
        }
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    def load_nvram(self, path: str) -> None:
        """Load settings from a JSON file; silently ignores missing or corrupt files."""
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            log.warning("nvram: could not read %s", path)
            return
        ch, p = self.tnc.channel, self.params
        try:
            if "mycall" in data:
                self.mgr.mycall = Address.parse(data["mycall"])
            for attr in ("monitor", "mcon", "echo", "conok"):
                if attr in data:
                    setattr(self, attr, bool(data[attr]))
            if "unproto" in data:
                self.unproto = self._parse_path(data["unproto"])
            if "btext" in data:
                self.btext = str(data["btext"])[:120]
            if "beacon_every" in data:
                self.beacon_every = max(0, int(data["beacon_every"])) * 10.0
            if "txdelay" in data:
                ch.txdelay = max(0, min(120, int(data["txdelay"]))) / 100.0
            if "persist" in data:
                ch.persist = max(0, min(255, int(data["persist"])))
            if "slottime" in data:
                ch.slottime = max(0, min(250, int(data["slottime"]))) / 100.0
            if "frack" in data:
                p.frack = float(max(1, min(15, int(data["frack"]))))
            if "maxframe" in data:
                p.maxframe = max(1, min(7, int(data["maxframe"])))
            if "paclen" in data:
                p.paclen = max(1, min(256, int(data["paclen"])))
            if "retry" in data:
                p.retry = max(0, min(15, int(data["retry"])))
            if "max_streams" in data:
                self.max_streams = max(1, min(10, int(data["max_streams"])))
                if self.current_stream >= self.max_streams:
                    self.current_stream = 0
        except Exception as exc:
            log.warning("nvram: error applying %s: %s", path, exc)

    @staticmethod
    def _parse_path(arg: str) -> tuple[Address, list[Address]]:
        """``K1ABC``, ``K1ABC VIA RELAY,WIDE2-1`` or ``K1ABC V RELAY WIDE2-1``."""
        parts = arg.replace(",", " ").split()
        call = Address.parse(parts[0])
        rest = parts[1:]
        if rest and rest[0].upper() in ("VIA", "V"):
            rest = rest[1:]
        if len(rest) > 8:
            raise ValueError("at most 8 digipeaters")
        return call, [Address.parse(p) for p in rest]
