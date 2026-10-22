"""AMTOR/SITOR terminal on an audio leg: the ARQ station (mode A), the FEC transmitter and receiver (mode B), and a
plain-text host port (a pty or a TCP socket) to type into and read from.

Text typed on the port is sent (ARQ when linked, FEC when in FEC mode); received text is written to it. Lines that
begin with ``/`` are commands::

    /connect CALL   call a station (ARQ)        /disconnect   end the link        /turn   change direction (+?)
    /fec            broadcast what you type     /arq          back to standby     /status /selcal /help

Typing ``+?`` as the last characters of a line also changes direction, as on a real terminal.
"""

from __future__ import annotations

import logging
import select
import signal
import time
from collections import deque

import numpy as np

from . import code, fsk
from .arq import ArqStation
from .fec import SLOT, FecReceiver, FecTransmitter

log = logging.getLogger("hammodem.amtor")

HELP = ("/connect CALL | /disconnect | /turn | /fec | /arq | /status | /selcal | /help\r\n"
        "Anything else is traffic; a line ending in +? changes direction.\r\n")


class AmtorTnc:
    def __init__(self, mycall: str, fs: int = fsk.FS, write=None, center: float = fsk.CENTER,
                 shift: float = fsk.SHIFT, reply_ms: int = 50) -> None:
        self.write = write or (lambda b: None)
        self.fs = fs
        self.arq = ArqStation(mycall, fs, center, shift, reply_ms=reply_ms, on_text=self._rx_text,
                              on_event=self._event)
        self.fec_rx = FecReceiver(center, shift)
        self.fec_tx = FecTransmitter()
        self.fec_mod = fsk.Modulator(center, shift)
        self.mode = "arq"                       # arq | fec
        self._fec_audio = np.zeros(0, np.int16)
        self._line = ""
        self._rxq: deque[np.ndarray] = deque()
        self._head = np.zeros(0)
        self._skip_lf = False

    # host side ----------------------------------------------------------------------------------

    def _out(self, s: str) -> None:
        s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
        self.write(s.encode("latin-1", "replace"))

    def _rx_text(self, s: str) -> None:
        self.write(s.replace("\r", "").replace("\n", "\r\n").encode("latin-1", "replace"))

    def _event(self, e: str) -> None:
        self._out(f"\r\n*** {e}\r\n")

    def feed_host(self, data: bytes) -> None:
        for ch in data.decode("latin-1"):
            if ch == "\n" and self._skip_lf:                 # the LF of a CR LF pair
                self._skip_lf = False
                continue
            self._skip_lf = ch == "\r"
            if ch in "\r\n":
                self._submit(self._line)
                self._line = ""
            elif ch == "\x7f" or ch == "\b":
                self._line = self._line[:-1]
            else:
                self._line += ch

    def _submit(self, line: str) -> None:
        if line.startswith("/"):
            self._command(line[1:].split())
            return
        turn = line.rstrip().endswith("+?")
        text = (line.rstrip()[:-2] if turn else line) + "\r\n"
        if self.mode == "fec":
            self.fec_tx.send(text)
        elif self.arq.state in ("iss", "irs", "ending"):
            self.arq.send(text)
            if turn:
                self.arq.turn()
        else:
            self._out("*** not linked: /connect CALL or /fec first\r\n")

    def _command(self, w: list[str]) -> None:
        if not w:
            return
        cmd, args = w[0].lower(), w[1:]
        if cmd == "connect" and args:
            self.mode = "arq"
            self.arq.connect(args[0])
        elif cmd == "disconnect":
            self.arq.disconnect()
        elif cmd == "turn":
            self.arq.turn()
        elif cmd == "fec":
            self.mode = "fec"
            self._out("*** FEC (broadcast) mode: what you type is sent to all\r\n")
        elif cmd == "arq":
            self.mode = "arq"
        elif cmd == "selcal":
            self._out(f"{self.arq.selcal}\r\n")
        elif cmd == "status":
            self._out(f"{self.mode} state={self.arq.state} stats={self.arq.stats} fec lost={self.fec_rx.lost} "
                      f"corrected={self.fec_rx.corrected}\r\n")
        else:
            self._out(HELP)

    # audio side ---------------------------------------------------------------------------------

    MAX_BACKLOG = fsk.FS // 5

    def audio_in(self, block: np.ndarray) -> None:
        self._rxq.append(np.asarray(block, dtype=float))
        while sum(len(b) for b in self._rxq) > self.MAX_BACKLOG and len(self._rxq) > 1:
            self._rxq.popleft()                       # clock skew: drop the oldest

    def _take(self, n: int) -> np.ndarray:
        parts, need = [self._head], n - len(self._head)
        while need > 0 and self._rxq:
            b = self._rxq.popleft()
            parts.append(b)
            need -= len(b)
        x = np.concatenate(parts)
        if len(x) < n:
            x = np.concatenate((x, np.zeros(n - len(x))))
        self._head = x[n:]
        return x[:n]

    def audio_out(self, n: int) -> np.ndarray:
        rx = self._take(n)
        text = self.fec_rx.feed(rx) if self.arq.state == "standby" else ""
        if text:
            self._rx_text(text)
        out = self.arq.process(rx)
        if self.mode == "fec" and self.arq.state == "standby":
            while len(self._fec_audio) < n:
                c = self.fec_tx.next_slot()
                if c is None:
                    break
                self._fec_audio = np.concatenate((self._fec_audio, self.fec_mod.bits(code.bits(c))))
            if len(self._fec_audio):
                take = self._fec_audio[:n]
                self._fec_audio = self._fec_audio[n:]
                out = out.copy()
                out[:len(take)] += take
        return out


def run(tnc: AmtorTnc, audio, ports) -> None:
    """Clock the TNC from the leg's send tick; ``ports`` are PtySerial/TcpSerial objects (text in/out)."""
    def fanout(data: bytes) -> None:
        for p in ports:
            p.write(data)
    tnc.write = fanout
    n = audio.frame_samples
    tick = n / tnc.fs
    next_tx = time.monotonic()
    running = [True]
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: running.__setitem__(0, False))
    fds = lambda: list(audio.fds()) + [f for p in ports for f in p.fds()]   # noqa: E731
    try:
        while running[0]:
            try:
                r, w, _ = select.select(fds(), [f for p in ports for f in p.write_fds()], [],
                                        max(0.0, next_tx - time.monotonic()))
            except InterruptedError:
                continue
            for p in ports:
                data, _ = p.service(r, w)
                if data:
                    tnc.feed_host(data)
            for block in audio.service(r):
                tnc.audio_in(block)
            now = time.monotonic()
            if now - next_tx > 0.2:
                next_tx = now
            while now >= next_tx:
                audio.send(tnc.audio_out(n))
                next_tx += tick
    finally:
        audio.close()
        for p in ports:
            p.close()
