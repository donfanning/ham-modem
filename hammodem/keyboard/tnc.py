"""Keyboard-mode terminal on an audio leg: type text, it is sent as audio; received text appears on the same
port (pty or TCP). Half duplex by courtesy: nothing is sent while the queue is empty.

``mod`` needs ``text(str) -> int16 samples`` and ``idle_samples(seconds)``; ``demod`` needs ``feed(samples) -> str``.
"""

from __future__ import annotations

from collections import deque

import numpy as np

from ..amtor.tnc import run as _run
from ..amtor.tnc import AmtorTnc


class KeyboardTnc:
    LEAD, TAIL = 0.3, 0.25

    def __init__(self, name: str, mod, demod, fs: int = 8000, write=None) -> None:
        self.name, self.mod, self.demod, self.fs = name, mod, demod, fs
        self.write = write or (lambda b: None)
        self._tx = np.zeros(0, np.int16)
        self._line = ""
        self._skip_lf = False
        self._rxq: deque[np.ndarray] = deque()
        self._head = np.zeros(0)
        self.sent_chars = 0

    # host side
    def feed_host(self, data: bytes) -> None:
        for ch in data.decode("latin-1"):
            if ch == "\n" and self._skip_lf:
                self._skip_lf = False
                continue
            self._skip_lf = ch == "\r"
            if ch in "\r\n":
                self._submit(self._line)
                self._line = ""
            elif ch in "\x7f\b":
                self._line = self._line[:-1]
            else:
                self._line += ch

    def _submit(self, line: str) -> None:
        if line.startswith("/"):
            self._command(line[1:].split())
            return
        if not len(self._tx):
            self._tx = np.concatenate((self._tx, self.mod.idle_samples(self.LEAD)))
        self._tx = np.concatenate((self._tx, self.mod.text(line + "\r\n"), self.mod.idle_samples(self.TAIL)))
        self.sent_chars += len(line) + 2

    def _command(self, w: list[str]) -> None:
        if w and w[0].lower() == "status":
            self._reply(f"{self.name} sent {self.sent_chars} chars, {getattr(self.demod, 'chars', 0)} heard, "
                        f"{getattr(self.demod, 'errors', 0)} framing errors\r\n")
        else:
            self._reply("/status | /help; anything else is sent as text\r\n")

    def _reply(self, s: str) -> None:
        self.write(s.encode("latin-1", "replace"))

    # audio side
    def audio_in(self, block: np.ndarray) -> None:
        AmtorTnc.audio_in(self, block)

    MAX_BACKLOG = 8000 // 5

    def audio_out(self, n: int) -> np.ndarray:
        rx = AmtorTnc._take(self, n)
        text = self.demod.feed(rx)
        if text:
            self.write(text.replace("\r", "").replace("\n", "\r\n").encode("latin-1", "replace"))
        out = np.zeros(n, np.int16)
        k = min(n, len(self._tx))
        out[:k] = self._tx[:k]
        self._tx = self._tx[k:]
        return out


def run(tnc: KeyboardTnc, audio, ports) -> None:
    _run(tnc, audio, ports)
