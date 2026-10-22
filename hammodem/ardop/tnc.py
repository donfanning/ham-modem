"""ARDOP TNC on an audio leg: an ArqStation, its ardopcf-compatible host interface, and the loop that clocks it.

The outgoing block tick is the clock (as for the AX.25 TNC): every tick the station consumes one block of received
audio (buffered from the leg, silence if none arrived) and produces one block to send.
"""

from __future__ import annotations

import logging
import select
import time
from collections import deque

import numpy as np

from .arq import ArqStation
from .host import HostServer

log = logging.getLogger("hammodem.ardop")

MAX_RX_BACKLOG_MS = 200       # received audio waiting for our tick; older audio is dropped (clock skew)


class ArdopTnc:
    def __init__(self, station: ArqStation) -> None:
        self.st = station
        self.fs = station.fs
        self._rx: deque[np.ndarray] = deque()
        self._rx_len = 0
        self._head = np.zeros(0)

    def audio_in(self, block: np.ndarray) -> None:
        self._rx.append(np.asarray(block, dtype=float))
        self._rx_len += len(block)
        limit = self.fs * MAX_RX_BACKLOG_MS // 1000
        while self._rx_len - len(self._rx[0]) > limit:
            self._rx_len -= len(self._rx.popleft())

    def _take(self, n: int) -> np.ndarray:
        parts, need = [self._head], n - len(self._head)
        while need > 0 and self._rx:
            b = self._rx.popleft()
            self._rx_len -= len(b)
            parts.append(b)
            need -= len(b)
        x = np.concatenate(parts)
        if len(x) < n:
            x = np.concatenate((x, np.zeros(n - len(x))))
        self._head = x[n:]
        return x[:n]

    def audio_out(self, n: int) -> np.ndarray:
        return self.st.process(self._take(n))


def run(station: ArqStation, audio, host: str, port: int) -> None:
    tnc = ArdopTnc(station)
    server = HostServer(station, host, port)
    log.info("ARDOP %s at %d Hz; audio %s; host interface %s", station.mycall, station.fs, audio.name, server.name)
    n = audio.frame_samples
    tick = n / station.fs
    next_tx = time.monotonic()
    running = [True]
    import signal  # noqa: PLC0415
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: running.__setitem__(0, False))
    try:
        while running[0]:
            try:
                r, w, _ = select.select(list(audio.fds()) + server.fds(), server.write_fds(), [],
                                        max(0.0, next_tx - time.monotonic()))
            except InterruptedError:
                continue
            server.service(r, w)
            for block in audio.service(r):
                tnc.audio_in(block)
            now = time.monotonic()
            if now - next_tx > 0.2:
                log.warning("audio clock slipped %.0f ms; resyncing", (now - next_tx) * 1000)
                next_tx = now
            while now >= next_tx:
                audio.send(tnc.audio_out(n))
                next_tx += tick
    finally:
        server.close()
        audio.close()
        log.info("stats: %s; modes sent %s", station.stats, station.modes_used)
        for row in station.trace:
            log.debug("heard %s", row)
