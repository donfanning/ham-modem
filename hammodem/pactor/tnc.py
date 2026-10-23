"""PACTOR-I terminal on an audio leg: text interface with commands, following the AMTOR TNC pattern.

    /connect CALL   start a PACTOR link         /disconnect   end the link
    /speed          toggle 100/200 bd            /status       show stats
    /fec            FEC (broadcast) mode         /arq          back to standby
    /help

Text typed on the port is sent when linked; received text is written back.
"""

from __future__ import annotations

import logging
import select
import signal
import time
from collections import deque

import numpy as np

from . import code, fsk
from .arq import PactorStation

log = logging.getLogger("hammodem.pactor")

HELP = ("/connect CALL | /disconnect | /speed | /status | /fec | /arq | /help\r\n"
        "Type text when linked; it is sent in the next packet.\r\n")


class PactorTnc:
    def __init__(self, mycall: str, fs: int = fsk.FS, write=None, center: float = fsk.CENTER,
                 shift: float = fsk.SHIFT, longpath: bool = False) -> None:
        self.write = write or (lambda b: None)
        self.fs = fs
        self.center, self.shift = center, shift
        self.station = PactorStation(mycall, fs, center, shift, longpath=longpath,
                                     on_text=self._rx_text, on_event=self._event)
        self.mode = "arq"
        self._fec_audio = np.zeros(0, np.int16)
        self._fec_queue = bytearray()
        self._line = ""
        self._rxq: deque[np.ndarray] = deque()
        self._head = np.zeros(0)
        self._skip_lf = False

    def _out(self, s: str) -> None:
        s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
        self.write(s.encode("latin-1", "replace"))

    def _rx_text(self, s: str) -> None:
        self.write(s.replace("\r", "").replace("\n", "\r\n").encode("latin-1", "replace"))

    def _event(self, e: str) -> None:
        self._out(f"\r\n*** {e}\r\n")

    def feed_host(self, data: bytes) -> None:
        for ch in data.decode("latin-1"):
            if ch == "\n" and self._skip_lf:
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
        text = line + "\r\n"
        if self.mode == "fec":
            self._fec_queue.extend(text.encode("latin-1", "replace"))
        elif self.station.state in ("iss", "irs", "ending"):
            self.station.send(text)
        else:
            self._out("*** not linked: /connect CALL or /fec first\r\n")

    def _command(self, w: list[str]) -> None:
        if not w:
            return
        cmd, args = w[0].lower(), w[1:]
        if cmd == "connect" and args:
            self.mode = "arq"
            self.station.connect(args[0])
        elif cmd == "disconnect":
            self.station.disconnect()
        elif cmd == "speed":
            if self.station.state in ("iss", "irs"):
                self.station.baud = 100 if self.station.baud == 200 else 200
                self._out(f"*** speed set to {self.station.baud} bd\r\n")
            else:
                self._out("*** not linked\r\n")
        elif cmd == "fec":
            self.mode = "fec"
            self._out("*** FEC mode: what you type is broadcast\r\n")
        elif cmd == "arq":
            self.mode = "arq"
        elif cmd == "status":
            self._out(f"{self.mode} state={self.station.state} baud={self.station.baud} "
                      f"stats={self.station.stats}\r\n")
        else:
            self._out(HELP)

    # audio side -------------------------------------------------------------------

    MAX_BACKLOG = fsk.FS // 5

    def audio_in(self, block: np.ndarray) -> None:
        self._rxq.append(np.asarray(block, dtype=float))
        while sum(len(b) for b in self._rxq) > self.MAX_BACKLOG and len(self._rxq) > 1:
            self._rxq.popleft()

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
        out = self.station.process(rx)
        if self.mode == "fec" and self.station.state == "standby" and (self._fec_queue or len(self._fec_audio)):
            out = self._fec_tx(out, n)
        return out

    def _fec_tx(self, out: np.ndarray, n: int) -> np.ndarray:
        while len(self._fec_audio) < n and self._fec_queue:
            data_len = DATA_200 = 20
            data = bytes(self._fec_queue[:data_len])
            del self._fec_queue[:data_len]
            data = data + bytes([code.IDLE] * (data_len - len(data)))
            pkt = code.build_packet(data, 0)
            m = fsk.Modulator(self.center if hasattr(self, "center") else fsk.CENTER,
                              self.shift if hasattr(self, "shift") else fsk.SHIFT,
                              self.station.level)
            hdr_bits = [(code.HEADER1 >> i) & 1 for i in range(8)]
            pkt_bits = []
            for byte in pkt:
                for i in range(8):
                    pkt_bits.append((byte >> i) & 1)
            audio = m.bits(hdr_bits + pkt_bits, 200)
            self._fec_audio = np.concatenate((self._fec_audio, audio))
        if len(self._fec_audio):
            take = self._fec_audio[:n]
            self._fec_audio = self._fec_audio[n:]
            out = out.copy()
            out[:len(take)] += take
        return out


def run(tnc: PactorTnc, audio, ports) -> None:
    """Clock the TNC from the leg's send tick; ``ports`` are PtySerial/TcpSerial objects."""
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
