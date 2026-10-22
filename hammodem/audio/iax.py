"""IAX2 audio leg: one call to (or from) an IAX2 PBX, G.711 mu-law, driven from the TNC's select() loop.

Uses the ``ham_modem_iax`` PyO3 binding (``native/iax``, from El-Modem) over the sans-I/O ``iax2`` crate, which
handles registration, call control, retransmission and mu-law voice framing. This adapter is after El-Modem's
``iax_transport.py``: it owns the UDP socket, plays received voice out of the binding's jitter buffer on a 20 ms
clock, and -- like ``SipLeg`` -- keeps a dialled call up (redial after a hangup) or answers incoming calls.

The ``iax2`` crate carries mu-law only, so IAX2 legs suit the 8 kHz AFSK modes; wideband modes need SIP/L16.
Frames the jitter buffer had to conceal are handed on as ``ConcealedPcm`` (FX.25 erasures).
"""

from __future__ import annotations

import logging
import socket
import time

import numpy as np

from . import g711
from .rtp import ConcealedPcm

log = logging.getLogger("hammodem.iax")

FRAME = 160                                   # 20 ms at 8 kHz
PLAYOUT_DELAY = 0.040


class IaxUnavailable(RuntimeError):
    pass


class IaxLeg:
    def __init__(self, server: str, user: str, secret: str, *, dial: str | None = None, listen: bool = False,
                 redial_after: float = 5.0, bind_host: str = "0.0.0.0") -> None:
        try:
            from ham_modem_iax import Client, MediaJitter  # noqa: PLC0415
        except ImportError as exc:
            raise IaxUnavailable("native IAX2 module missing: build native/iax (see README)") from exc
        if not dial and not listen:
            raise ValueError("IaxLeg needs dial= or listen=True")
        host, _, port = server.partition(":")
        family, socktype, proto, _, self.address = socket.getaddrinfo(host, int(port or 4569),
                                                                      type=socket.SOCK_DGRAM)[0]
        self.sock = socket.socket(family, socktype, proto)
        self.sock.bind(("::" if family == socket.AF_INET6 and bind_host == "0.0.0.0" else bind_host, 0))
        self.sock.setblocking(False)
        self.client = Client("ham-modem", user, secret)
        self.jitter = MediaJitter()
        self.number, self.listen, self.redial_after = dial, listen, redial_after
        self.frame_samples = FRAME
        self.rate = 8000
        self.name = f"iax2:{dial}@{server}" if dial else f"iax2 listen as {user}@{server}"
        self.state = "idle"
        self.call: int | None = None
        self.registered = False
        self._next_dial = time.monotonic() + 1.0          # let registration go first
        self._play_at: float | None = None
        self._last = np.zeros(FRAME, np.int16)
        self.stats = {"rx_frames": 0, "tx_frames": 0, "concealed": 0}
        self._flush()

    # ---------------------------------------------------------------- leg interface

    def fds(self):
        return [self.sock]

    def service(self, readable) -> list[np.ndarray]:
        self.client.tick()
        if self.sock in readable:
            while True:
                try:
                    data, _ = self.sock.recvfrom(4096)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    continue
                self.client.input(data)
        self._flush()
        for ev in self.client.drain_events():
            self._event(ev)
        now = time.monotonic()
        if self.number and self.state == "idle" and now >= self._next_dial:
            self.state = "calling"
            self.jitter.reset()
            self.client.command("dial", self.number)
            self._flush()
        return self._playout(now)

    def send(self, pcm: np.ndarray) -> None:
        if self.state == "up" and self.call is not None:
            self.client.voice(self.call, g711.encode(pcm, "pcmu").astype(np.uint8).tobytes())
            self.stats["tx_frames"] += 1
            self._flush()

    def close(self) -> None:
        if self.call is not None:
            self.client.command("hangup", str(self.call))
            self._flush()
        self.sock.close()

    # ---------------------------------------------------------------- internals

    def _flush(self) -> None:
        for datagram in self.client.drain_tx():
            try:
                self.sock.sendto(datagram, self.address)
            except OSError as exc:
                log.debug("IAX send failed: %s", exc)

    def _event(self, ev: dict) -> None:
        kind = ev["type"]
        if kind == "voice":
            if ev.get("call") == self.call:
                self.jitter.push(ev["ts"], ev.get("ulaw", b""))
                self.stats["rx_frames"] += 1
            return
        if kind == "registered":
            if not self.registered:
                log.info("%s: registered", self.name)
            self.registered = True
        elif kind == "register_lost":
            self.registered = False
            log.warning("%s: registration lost (%s)", self.name, ev.get("reason"))
        elif kind == "dialing":
            self.call = ev["call"]
        elif kind == "incoming":
            if self.listen and self.state == "idle":
                self.call = ev["call"]
                self.client.command("answer", str(self.call))
                self._flush()
                self._up(f"answered call from {ev.get('from')}")
            else:
                self.client.command("hangup", str(ev["call"]))
                self._flush()
        elif kind == "answered" and ev.get("call") == self.call:
            self._up("answered")
        elif kind == "ended" and ev.get("call") == self.call:
            self.call, self.state, self._play_at = None, "idle", None
            self._next_dial = time.monotonic() + self.redial_after
            log.info("%s: idle (%s)", self.name, ev.get("reason"))
        elif kind == "log":
            log.debug("iax2: %s", ev.get("message"))

    def _up(self, why: str) -> None:
        self.state = "up"
        self.jitter.reset()
        self._play_at = None
        log.info("%s: up (%s, PCMU/8000)", self.name, why)

    def _playout(self, now: float) -> list[np.ndarray]:
        if self.state != "up":
            return []
        if self._play_at is None:
            self._play_at = now + PLAYOUT_DELAY
            return []
        out: list[np.ndarray] = []
        while now >= self._play_at:
            kind, payload = self.jitter.pull()
            if kind == "play" and payload:
                pcm = g711.decode(np.frombuffer(payload, np.uint8), "pcmu").astype(np.int16)
                self._last = pcm
                out.append(pcm)
            elif kind == "conceal":
                self.stats["concealed"] += 1
                out.append(self._last.copy().view(ConcealedPcm))
            else:
                out.append(np.zeros(FRAME, np.int16))      # silence: nothing queued
            self._play_at += 0.020
        return out
