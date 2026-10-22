"""ardopcf-compatible host interface: TCP command port (N) and data port (N+1), as Winlink clients (Pat, Winlink
Express, BPQ) expect from an ARDOP TNC.

* Command port: CR-terminated ASCII lines. Commands are case-insensitive; a query (no argument) answers
  ``CMD value``, a setting answers ``CMD now value``, errors answer ``FAULT ...``. Asynchronous notices
  (``NEWSTATE``, ``CONNECTED``, ``DISCONNECTED``, ``BUFFER``, ``PTT``, ``STATUS``, ``PENDING``, ...) arrive on the same
  port.
* Data port: host -> TNC is ``len_hi len_lo data``; TNC -> host is ``len_hi len_lo TAG data`` with TAG ``ARQ``
  (connected data, in sequence) or ``IDF`` (a decoded ID frame), the length counting the tag.

One host connection per port (as ardopcf); a new connection replaces the old one.
"""

from __future__ import annotations

import logging
import socket

from .arq import FRAME_BY_NAME, ArqStation

log = logging.getLogger("hammodem.ardop.host")

ARQBW = ("200FORCED", "500FORCED", "1000FORCED", "2000FORCED", "200MAX", "500MAX", "1000MAX", "2000MAX")
VERSION = "ham-modem-ardop_0.1"


class _Port:
    def __init__(self, host: str, port: int, kind: str) -> None:
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((host, port))
        self.srv.listen(2)
        self.srv.setblocking(False)
        self.kind = kind
        self.conn: socket.socket | None = None
        self.inbuf = bytearray()
        self.outbuf = bytearray()

    def fds(self):
        return [self.srv] + ([self.conn] if self.conn else [])

    def write(self, data: bytes) -> None:
        if self.conn is None or len(self.outbuf) > 1 << 20:
            return
        self.outbuf += data
        self.flush()

    def flush(self) -> None:
        if not self.conn or not self.outbuf:
            return
        try:
            n = self.conn.send(self.outbuf)
            del self.outbuf[:n]
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            self.drop()

    def drop(self) -> None:
        if self.conn:
            self.conn.close()
        self.conn = None
        self.inbuf.clear()
        self.outbuf.clear()

    def service(self, readable) -> bytes:
        """Accept / read; returns newly received bytes."""
        if self.srv in readable:
            try:
                sock, addr = self.srv.accept()
            except OSError:
                sock = None
            if sock is not None:
                self.drop()
                sock.setblocking(False)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.conn = sock
                log.info("host %s connection from %s:%d", self.kind, *addr)
        if self.conn is not None and self.conn in readable:
            try:
                data = self.conn.recv(65536)
            except (BlockingIOError, InterruptedError):
                return b""
            except OSError:
                data = b""
            if not data:
                log.info("host %s connection closed", self.kind)
                self.drop()
            return data
        return b""


class HostServer:
    def __init__(self, station: ArqStation, host: str = "127.0.0.1", port: int = 8515) -> None:
        self.st = station
        self.cmd = _Port(host, port, "command")
        self.data = _Port(host, port + 1, "data")
        self.name = f"ardop-host://{host}:{port}+{port + 1}"
        self.st.on_event = self._notice
        self.st.on_data = self._deliver
        self.settings = {"CODEC": "FALSE", "LISTEN": "TRUE" if station.listen else "FALSE",
                         "AUTOBREAK": "TRUE" if station.autobreak else "FALSE",
                         "FASTSTART": "TRUE" if station.fast_start else "FALSE", "BUSYBLOCK": "FALSE",
                         "CWID": "FALSE", "ENABLEPINGACK": "TRUE", "MONITOR": "TRUE", "BUSYDET": "5",
                         "EXTRADELAY": "0", "TUNINGRANGE": "100", "USE600MODES": "FALSE", "FSKONLY": "FALSE",
                         "SQUELCH": "5", "CMDTRACE": "FALSE", "CALLBW": "UNDEFINED", "NEGOTIATEBW": "TRUE",
                         "DEBUGLOG": "FALSE", "CONSOLELOG": "4", "LOGLEVEL": "4", "MYAUX": "",
                         "CAPTURE": "voip", "PLAYBACK": "voip", "RADIOPTTON": "", "RADIOPTTOFF": "",
                         "RADIOHEX": "", "RADIOFREQ": ""}
        self.call_bw: str | None = None
        self._last_disc_at = -1e9
        self._ptt = False

    # ------------------------------------------------------------------ server plumbing

    def fds(self):
        return self.cmd.fds() + self.data.fds()

    def write_fds(self):
        return [p.conn for p in (self.cmd, self.data) if p.conn and p.outbuf]

    def service(self, readable, writable=()) -> None:
        for p in (self.cmd, self.data):
            if p.conn in writable:
                p.flush()
        self.cmd.inbuf += self.cmd.service(readable)
        while b"\r" in self.cmd.inbuf:
            line, _, rest = bytes(self.cmd.inbuf).partition(b"\r")
            self.cmd.inbuf = bytearray(rest)
            text = line.decode("latin-1").strip("\n ")
            if text:
                self.command(text)
        self.data.inbuf += self.data.service(readable)
        while len(self.data.inbuf) >= 2:
            n = int.from_bytes(self.data.inbuf[:2], "big")
            if len(self.data.inbuf) < 2 + n:
                break
            payload = bytes(self.data.inbuf[2:2 + n])
            del self.data.inbuf[:2 + n]
            if payload:
                self.st.send(payload)

    def close(self) -> None:
        for p in (self.cmd, self.data):
            p.drop()
            p.srv.close()

    def reply(self, text: str) -> None:
        self.cmd.write(text.encode("latin-1") + b"\r")

    def _notice(self, text: str) -> None:
        if text == "NEWSTATE DISC":
            self._last_disc_at = self.st.now
        if not text.startswith(("PTT", "BUFFER")):
            log.debug("host< %s", text)
        self.reply(text)

    def _deliver(self, tag: str, data: bytes) -> None:
        payload = tag.encode() + data
        self.data.write(len(payload).to_bytes(2, "big") + payload)

    # ------------------------------------------------------------------ commands

    def command(self, line: str) -> None:
        log.debug("host> %s", line)
        name, _, arg = line.partition(" ")
        cmd, arg = name.upper(), arg.strip()
        try:
            fault = self._dispatch(cmd, arg, line)
        except ValueError as exc:
            fault = f"Syntax Err: {line}: {exc}"
        if fault:
            log.info("host> %s -> FAULT %s", line, fault)
            self.reply(f"FAULT {fault}")

    def _flag(self, cmd: str, arg: str) -> str | None:
        if not arg:
            self.reply(f"{cmd} {self.settings[cmd]}")
            return None
        v = arg.upper()
        if v not in ("TRUE", "FALSE"):
            return f"Syntax Err: {cmd} {arg}"
        self.settings[cmd] = v
        self.reply(f"{cmd} now {v}")
        return None

    def _dispatch(self, cmd: str, arg: str, line: str) -> str | None:
        st = self.st
        if cmd in ("ABORT", "DD"):
            st.abort()
            self.reply("ABORT")
        elif cmd == "INITIALIZE":
            st.purge()
            self.reply("INITIALIZE")
        elif cmd == "VERSION":
            self.reply(f"VERSION {VERSION}")
        elif cmd == "STATE":
            self.reply(f"STATE {st.state}")
        elif cmd == "BUFFER":
            self.reply(f"BUFFER {len(st.outq)}")
        elif cmd == "PURGEBUFFER":
            st.purge()
            self.reply("PURGEBUFFER")
        elif cmd == "MYCALL":
            if not arg:
                self.reply(f"MYCALL {st.mycall}")
            else:
                call = arg.upper().split()[0]
                st.mycall = call
                self.reply(f"MYCALL now {call}")
        elif cmd == "GRIDSQUARE":
            if not arg:
                self.reply(f"GRIDSQUARE {st.grid}")
            else:
                st.grid = arg.upper()
                self.reply(f"GRIDSQUARE now {st.grid}")
        elif cmd == "ARQBW":
            if not arg:
                self.reply(f"ARQBW {st.bandwidth}")
            elif arg.upper() not in ARQBW:
                return f"Syntax Err: {line}"
            else:
                st.bandwidth = arg.upper()
                self.reply(f"ARQBW now {st.bandwidth}")
        elif cmd == "CALLBW":
            if not arg:
                self.reply(f"CALLBW {self.call_bw or 'UNDEFINED'}")
            elif arg.upper() == "UNDEFINED":
                self.call_bw = None
                self.reply("CALLBW now UNDEFINED")
            elif arg.upper() in ARQBW:
                self.call_bw = arg.upper()
                self.reply(f"CALLBW now {self.call_bw}")
            else:
                return f"Syntax Err: {line}"
        elif cmd == "ARQTIMEOUT":
            if not arg:
                self.reply(f"ARQTIMEOUT {st.timeout_ms // 1000}")
            else:
                st.timeout_ms = max(30, min(600, int(arg))) * 1000
                self.reply(f"ARQTIMEOUT now {st.timeout_ms // 1000}")
        elif cmd in ("LEADER", "TRAILER"):
            attr = "leader_ms" if cmd == "LEADER" else "trailer_ms"
            if not arg:
                self.reply(f"{cmd} {getattr(st, attr)}")
            else:
                setattr(st, attr, max(0, min(2000, (int(arg) + 9) // 10 * 10)))
                self.reply(f"{cmd} now {getattr(st, attr)}")
        elif cmd == "DRIVELEVEL":
            if not arg:
                self.reply(f"DRIVELEVEL {round(st.drive * 100)}")
            else:
                st.drive = max(0, min(100, int(arg))) / 100
                self.reply(f"DRIVELEVEL now {round(st.drive * 100)}")
        elif cmd == "PROTOCOLMODE":
            if not arg:
                self.reply(f"PROTOCOLMODE {st.protocol_mode}")
            elif arg.upper() in ("ARQ", "FEC"):
                st.protocol_mode = arg.upper()
                self.reply(f"PROTOCOLMODE now {st.protocol_mode}")
                st.abort()                                   # ardopcf: any protocol mode change goes to DISC
            else:
                return f"PROTOCOLMODE {arg.upper()} not supported (ARQ and FEC only)"
        elif cmd == "FECMODE":
            if not arg:
                self.reply(f"FECMODE {st.fec_mode}")
            elif arg + ".E" in FRAME_BY_NAME:
                st.fec_mode = arg
                self.reply(f"FECMODE now {arg}")
            else:
                return f"Syntax Err: FECMODE {arg}"
        elif cmd == "FECREPEATS":
            if not arg:
                self.reply(f"FECREPEATS {st.fec_repeats}")
            elif arg.isdigit() and int(arg) <= 5:
                st.fec_repeats = int(arg)
                self.reply(f"FECREPEATS now {arg}")
            else:
                return f"Syntax Err: FECREPEATS {arg}"
        elif cmd == "FECID":
            if not arg:
                self.reply(f"FECID {str(st.fec_id).upper()}")
            elif arg.upper() in ("TRUE", "FALSE"):
                st.fec_id = arg.upper() == "TRUE"
                self.reply(f"FECID now {arg.upper()}")
            else:
                return f"Syntax Err: FECID {arg}"
        elif cmd == "FECSEND":
            if arg.upper() == "TRUE":
                if not st.mycall:
                    return "MYCALL not set"
                if not st.fec_start():
                    return "StartFEC failed for FECSEND TRUE."
                self.reply("FECSEND now TRUE")
            elif arg.upper() == "FALSE":
                st.fec_abort()
                self.reply("FECSEND now FALSE")
            else:
                return f"Syntax Err: FECSEND {arg}".rstrip()
        elif cmd == "ARQCALL":
            parts = arg.split()
            if len(parts) != 2 or not parts[1].isdigit():
                return f"Syntax Err: {line}"
            if st.protocol_mode == "FEC":
                return "Not from mode FEC"
            if st.state != "DISC":
                return f"Not from state {st.state}"
            st.conreq_repeats = max(2, min(15, int(parts[1])))
            st.connect(parts[0], self.call_bw)
            self.reply(line)
        elif cmd == "DISCONNECT":
            if st.state in ("IDLE", "IRS", "ISS", "IRStoISS"):
                st.disconnect()
                self.reply("DISCONNECT NOW TRUE")
            else:
                self.reply("DISCONNECT IGNORED")
                # Deviation from ardopcf: Pat (wl2k-go) may send a second DISCONNECT after the session has already
                # ended and then waits 30 s for DISCONNECTED / NEWSTATE DISC before giving up with ABORT. Repeating
                # the (unchanged) state shortly after a session ends answers it at once.
                if st.now - self._last_disc_at < 10_000:
                    self.reply("NEWSTATE DISC")
        elif cmd == "BREAK":
            st.break_()
            self.reply("BREAK")
        elif cmd == "PING":
            parts = arg.split()
            if len(parts) != 2 or not parts[1].isdigit():
                return f"Syntax Err: {line}"
            if st.state != "DISC":
                return f"No PING from state {st.state}"
            st.ping(parts[0], int(parts[1]))
            self.reply(line)
        elif cmd == "SENDID":
            if st.state != "DISC":
                return f"Not from State {st.state}"
            st.send_id()
            self.reply("SENDID")
        elif cmd == "LISTEN":
            fault = self._flag(cmd, arg)
            st.listen = self.settings["LISTEN"] == "TRUE"
            return fault
        elif cmd == "AUTOBREAK":
            fault = self._flag(cmd, arg)
            st.autobreak = self.settings["AUTOBREAK"] == "TRUE"
            return fault
        elif cmd == "FASTSTART":
            fault = self._flag(cmd, arg)
            st.fast_start = self.settings["FASTSTART"] == "TRUE"
            return fault
        elif cmd == "USE600MODES":
            fault = self._flag(cmd, arg)
            st.fm600 = self.settings["USE600MODES"] == "TRUE"
            return fault
        elif cmd in ("CODEC", "BUSYBLOCK", "CWID", "ENABLEPINGACK", "MONITOR", "FSKONLY", "NEGOTIATEBW",
                     "CMDTRACE", "DEBUGLOG"):
            return self._flag(cmd, arg)
        elif cmd in self.settings:                       # accepted for compatibility, no effect here
            if not arg:
                self.reply(f"{cmd} {self.settings[cmd]}")
            else:
                self.settings[cmd] = arg
                self.reply(f"{cmd} now {arg}")
        elif cmd in ("CAPTUREDEVICES", "PLAYBACKDEVICES"):
            self.reply(f"{cmd} voip")
        elif cmd in ("TWOTONETEST", "TXFRAME", "DATATOSEND"):
            return f"{cmd} not supported"
        else:
            return f"CMD {cmd} not recoginized"                # (sic: ardopcf's spelling)
        return None
