"""Working stop-and-wait ARQ for the HF keyboard modes.

The physical layers in ``pactor2``, ``pactor3`` and ``clover2`` are burst
oriented.  This module supplies the missing piece: a link state machine that
finds bursts in a stream, sends and acknowledges frames, retries on loss, and
turns the result into a text terminal for ``main.py``.

Frames are deliberately simple.  They are *not* SCS/HAL framing; they make two
ham-modem stations interoperate with each other and give the modems a real
workout over lossy audio.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .burst import pop_burst

# Data-burst control values, carried in the frame status byte.
F_DATA = 0
F_CONNECT = 1
F_DISC = 2
F_TURN = 3
F_IDLE = 4

# Control-signal values (12-bit CS for PACTOR; a full frame for Clover).
CS_ACK = 1
CS_NACK = 2
CS_CONNECT = 3
CS_DISC = 4
CS_BUSY = 5
CS_TURN = 6


def pack_status(kind: int, seq: int = 0) -> int:
    return (kind & 0x07) | ((seq & 1) << 3)


def unpack_status(value: int) -> tuple[int, int]:
    return value & 0x07, (value >> 3) & 1


def pack_control(kind: int, seq: int = 0, arg: int = 0) -> int:
    return ((kind & 0x0F) << 8) | ((seq & 1) << 7) | (arg & 0x7F)


def unpack_control(value: int) -> tuple[int, int, int]:
    return (value >> 8) & 0x0F, (value >> 7) & 1, value & 0x7F


@dataclass
class LinkStats:
    sent: int = 0
    retries: int = 0
    received: int = 0
    bad: int = 0


class LinkBase:
    """A half-duplex stop-and-wait ARQ link over one burst modem.

    Subclasses provide the physical layer:

    ``make_data(payload, status) -> int16 audio``
    ``decode_data(audio) -> dict | None``
    ``make_control(kind, seq, arg) -> int16 audio``
    ``decode_control(audio) -> (kind, seq, arg) | None``
    """

    def __init__(self, mycall: str, fs: int = 8000, level: int = 2,
                 data_bytes: int = 20, burst_len: int = 8000,
                 control_len: int = 2160, cycle_len: int = 10160,
                 max_retries: int = 8,
                 on_text: Callable[[bytes], None] | None = None,
                 on_event: Callable[[str], None] | None = None) -> None:
        self.mycall = mycall.upper()
        self.fs = fs
        self.level = level
        self.data_bytes = data_bytes
        self._burst_len = burst_len
        self._control_len = control_len
        self._cycle_len = cycle_len
        self.max_retries = max_retries
        self.on_text = on_text or (lambda data: None)
        self.on_event = on_event or (lambda text: None)

        self.state = "idle"          # idle | connecting | connected | disconnecting
        self.role: str | None = None  # iss | irs
        self.remote = ""
        self.stats = LinkStats()

        self._outq: list[tuple[int, bytes]] = []
        self._current: tuple[int, int, bytes] | None = None
        self._waiting = False
        self._deadline = 0
        self._retries = 0
        self._seq = 0
        self._clock = 0
        self._rx = np.zeros(0, dtype=np.float64)
        self._tx = np.zeros(0, dtype=np.int16)

    # -- public API ---------------------------------------------------------

    def connect(self, call: str) -> None:
        if self.state != "idle":
            self._event(f"already {self.state}")
            return
        self.remote = call.upper()
        self.state = "connecting"
        self.role = "iss"
        payload = f"{self.mycall}\0{self.remote}".encode("latin-1", "replace")
        self._outq = [(F_CONNECT, payload)]
        self._current = None
        self._waiting = False
        self._event(f"calling {self.remote}")
        self._service_tx()

    def disconnect(self) -> None:
        if self.state == "idle":
            return
        self.state = "disconnecting"
        self.role = "iss"
        self._outq = [(F_DISC, b"")]
        self._current = None
        self._waiting = False
        self._service_tx()

    def send(self, data: bytes) -> None:
        if self.state != "connected":
            self._event("not linked: /connect CALL")
            return
        if not data:
            return
        if self.role == "irs" and (not self._outq or self._outq[0][0] != F_TURN):
            # Ask for the turn, then queue the traffic behind it.
            self._outq.append((F_TURN, b""))
        for i in range(0, len(data), self.data_bytes):
            self._outq.append((F_DATA, data[i:i + self.data_bytes]))
        self._service_tx()

    def turn(self) -> None:
        if self.state != "connected":
            self._event("not linked")
            return
        if not self._outq or self._outq[0][0] != F_TURN:
            self._outq.insert(0, (F_TURN, b""))
        self._service_tx()

    def tick(self, rx: np.ndarray, n: int) -> np.ndarray:
        """Advance by ``n`` samples, consuming received audio and producing TX audio."""
        if len(rx):
            self._rx = np.concatenate((self._rx, np.asarray(rx, dtype=np.float64)))
        self._clock += n
        self._service_rx()
        self._service_tx()

        out = np.zeros(n, dtype=np.int16)
        if len(self._tx):
            take = min(n, len(self._tx))
            out[:take] = self._tx[:take]
            self._tx = self._tx[take:]
        return out

    # -- subclass hooks -----------------------------------------------------

    def make_data(self, payload: bytes, status: int) -> np.ndarray:
        raise NotImplementedError

    def decode_data(self, audio: np.ndarray) -> dict | None:
        raise NotImplementedError

    def make_control(self, kind: int, seq: int = 0, arg: int = 0) -> np.ndarray:
        raise NotImplementedError

    def decode_control(self, audio: np.ndarray) -> tuple[int, int, int] | None:
        raise NotImplementedError

    # -- receive ------------------------------------------------------------

    def _service_rx(self) -> None:
        if len(self._tx):
            return
        while True:
            length = self._control_len if self._waiting else self._burst_len
            got = pop_burst(self._rx, length, self.fs)
            if got is None:
                return
            burst, self._rx = got
            if self._waiting:
                self._handle_control(burst)
            else:
                self._handle_data(burst)
            if len(self._tx):
                return

    def _handle_control(self, audio: np.ndarray) -> None:
        value = self.decode_control(audio)
        if value is None:
            return
        kind, seq, arg = value
        current = self._current
        if current is None:
            return
        ctype, cseq, _ = current

        if kind == CS_NACK:
            self._retry("nack")
            return
        if kind == CS_BUSY:
            self._event("remote busy")
            self._deadline = self._clock + self._cycle_len
            return

        if ctype == F_DATA and kind == CS_ACK and seq == cseq:
            self._complete_current()
        elif ctype == F_CONNECT and kind == CS_CONNECT:
            self._complete_current()
        elif ctype == F_DISC and kind == CS_DISC:
            self._complete_current()
        elif ctype == F_TURN and kind == CS_TURN:
            self._complete_current()

    def _handle_data(self, audio: np.ndarray) -> None:
        result = self.decode_data(audio)
        if result is None:
            self.stats.bad += 1
            if self.state == "connected" and self.role == "irs":
                self._queue_control(CS_NACK, self._seq)
            return

        status = int(result.get("status", 0))
        kind, seq = unpack_status(status)
        payload = bytes(result.get("data", b"")).rstrip(b"\x00")
        self.stats.received += 1

        if kind == F_CONNECT:
            if self.state != "idle":
                self._queue_control(CS_BUSY)
                return
            caller, _, target = payload.partition(b"\0")
            caller_s = caller.decode("latin-1", "replace").strip().upper()
            target_s = target.split(b"\0", 1)[0].decode("latin-1", "replace").strip().upper()
            target_s = target_s or self.mycall
            if target_s.split("-")[0] != self.mycall.split("-")[0]:
                self._queue_control(CS_BUSY)
                return
            self.remote = caller_s
            self.state = "connected"
            self.role = "irs"
            self._seq = 0
            self._queue_control(CS_CONNECT)
            self._event(f"connected to {self.remote}")

        elif kind == F_DATA:
            if self.state == "connected" and self.role == "irs":
                self.on_text(payload.rstrip(b"\x00"))
                self._queue_control(CS_ACK, seq)
            else:
                self._queue_control(CS_NACK, seq)

        elif kind == F_DISC:
            self.state = "idle"
            self.role = None
            self._queue_control(CS_DISC)
            self._event("disconnected")

        elif kind == F_TURN:
            self.role = "irs"
            self._queue_control(CS_TURN)
            self._event("turn")

        elif kind == F_IDLE:
            self._queue_control(CS_ACK, seq)

    # -- transmit -----------------------------------------------------------

    def _service_tx(self) -> None:
        if len(self._tx):
            return
        if self._current is not None and not self._waiting:
            self._send_current()
            return
        if self._waiting:
            if self._clock >= self._deadline:
                self._retry("timeout")
            return
        if not self._outq:
            return

        kind = self._outq[0][0]
        can_send = self.state in ("connecting", "disconnecting")
        if self.state == "connected":
            can_send = self.role == "iss" or (self.role == "irs" and kind == F_TURN)
        if not can_send:
            return

        ftype, payload = self._outq.pop(0)
        seq = 0 if ftype != F_DATA else self._seq
        self._current = (ftype, seq, payload)
        self._send_current()

    def _send_current(self) -> None:
        if self._current is None:
            return
        ftype, seq, payload = self._current
        audio = self.make_data(payload, pack_status(ftype, seq))
        self._tx = np.asarray(audio, dtype=np.int16)
        self._waiting = True
        self._deadline = self._clock + self._cycle_len
        self.stats.sent += 1

    def _retry(self, why: str) -> None:
        self.stats.retries += 1
        if self._retries >= self.max_retries:
            self._event(f"link failure ({why})")
            self.state = "idle"
            self.role = None
            self._current = None
            self._waiting = False
            self._outq.clear()
            return
        self._retries += 1
        self._event(f"retry {self._retries} ({why})")
        self._send_current()

    def _complete_current(self) -> None:
        if self._current is None:
            return
        ftype, _, _ = self._current
        if ftype == F_DATA:
            self._seq ^= 1
        elif ftype == F_CONNECT:
            self.state = "connected"
            self.role = "iss"
            self._event(f"connected to {self.remote}")
        elif ftype == F_DISC:
            self.state = "idle"
            self.role = None
            self._event("disconnected")
        elif ftype == F_TURN:
            self.role = "irs" if self.role == "iss" else "iss"
            self._event(f"turn ({self.role})")
        self._current = None
        self._waiting = False
        self._retries = 0
        self._service_tx()

    def _queue_control(self, kind: int, seq: int = 0, arg: int = 0) -> None:
        audio = self.make_control(kind, seq, arg)
        self._tx = np.concatenate((self._tx, np.asarray(audio, dtype=np.int16)))

    def _event(self, text: str) -> None:
        self.on_event(text)


class PactorLinkBase(LinkBase):
    """Common PACTOR-II/III link hooks."""

    def make_data(self, payload: bytes, status: int) -> np.ndarray:
        return self.mod.burst(payload, status)

    def decode_data(self, audio: np.ndarray) -> dict | None:
        return self.demod.feed_burst(audio)

    def make_control(self, kind: int, seq: int = 0, arg: int = 0) -> np.ndarray:
        return self.mod.cs_reply(pack_control(kind, seq, arg))

    def decode_control(self, audio: np.ndarray) -> tuple[int, int, int] | None:
        value = self.cs_decode(audio)
        if value is None:
            return None
        return unpack_control(value)

    def cs_decode(self, audio: np.ndarray) -> int | None:
        raise NotImplementedError
