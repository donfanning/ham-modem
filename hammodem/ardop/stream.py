"""ARDOP physical layer on an audio leg: resampling between the leg's rate and ARDOP's 12 kHz, and a streaming
receiver that turns arbitrary-sized blocks of samples into decoded frames.

ARDOP's widest frames reach ~2.5 kHz (8PSK/16QAM.2000, 600 baud 4FSK up to 2400 Hz + sidebands), so an 8 kHz
G.711 leg carries every mode; 16 and 48 kHz legs are resampled the same way.
"""

from __future__ import annotations

import math

import numpy as np

from . import modulate
from .codec import SAMPLE_RATE as FS
from .demod import LEADER_SYM, Decoded, Receiver


class Resampler:
    """Streaming rational resampler (up/down) with a Kaiser-windowed sinc low-pass; one instance per direction.
    Output is delayed by a constant (half the filter, in output samples)."""

    def __init__(self, up: int, down: int, taps_per_phase: int = 24) -> None:
        g = math.gcd(up, down)
        self.up, self.down = up // g, down // g
        n = taps_per_phase * max(self.up, self.down) | 1
        cutoff = 0.45 / max(self.up, self.down)                  # cycles per upsampled sample (0.9 of Nyquist)
        t = np.arange(n) - (n - 1) / 2
        self.h = 2 * cutoff * np.sinc(2 * cutoff * t) * np.kaiser(n, 8.0) * self.up
        self._hist = np.zeros(n - 1)
        self._phase = 0
        self.delay = (n - 1) / 2 / self.down                      # in output samples

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.up == self.down == 1:
            return np.asarray(x, dtype=float)
        u = np.zeros(len(x) * self.up)
        u[::self.up] = x
        buf = np.concatenate((self._hist, u))
        y = np.convolve(buf, self.h, "valid")
        out = y[self._phase::self.down]
        self._phase = (self._phase - len(y)) % self.down
        self._hist = buf[len(buf) - len(self._hist):]
        return out


class Phy:
    """One station's ARDOP modem on a leg at ``fs``: ``transmit(encoded)`` -> int16 samples at ``fs``;
    ``feed(samples)`` -> frames completed so far (``Decoded.start``/``end`` in 12 kHz samples since creation)."""

    KEEP = (Receiver.MIN_LEADER_SYMBOLS + 4) * LEADER_SYM       # history kept while no leader is in sight

    def __init__(self, fs: int, sessions: tuple[int, ...] = ()) -> None:
        self.fs = fs
        self.rx = Receiver(sessions)
        self._down = Resampler(FS, fs)                           # leg -> 12 kHz
        self._buf = np.zeros(0)
        self.recent = np.zeros(0)                                # last 1024 samples at 12 kHz (busy detector)
        self._offset = 0                                         # absolute 12 kHz index of _buf[0]
        self._scan = 0                                           # where the next search starts, within _buf
        self._need = 0                                           # buffer length a pending frame needs

    def busy(self) -> bool:
        """A frame (or leader) is arriving."""
        return self._need > 0

    def set_sessions(self, sessions: tuple[int, ...]) -> None:
        self.rx.sessions = (0xFF, *sessions)

    def transmit(self, encoded: bytes, leader_ms: int = 240, trailer_ms: int = 20) -> np.ndarray:
        x = modulate.modulate(encoded, leader_ms, trailer_ms).astype(float)
        if self.fs == FS:
            return x.astype(np.int16)
        up = Resampler(self.fs, FS)
        y = up.process(np.concatenate((x, np.zeros(int(2 * up.delay * FS / self.fs) + 1))))
        y = y[int(round(up.delay)):]
        return np.clip(np.round(y), -32768, 32767).astype(np.int16)

    def feed(self, samples: np.ndarray) -> list[Decoded]:
        new = self._down.process(np.asarray(samples, dtype=float))
        self.recent = np.concatenate((self.recent, new))[-1024:]
        self._buf = np.concatenate((self._buf, new))
        out: list[Decoded] = []
        while len(self._buf) >= self._need:
            self._need = 0
            d = self.rx.decode(self._buf, self._scan)
            if d is None:
                self._scan = max(self._scan, len(self._buf) - self.KEEP)
                break
            if d.extra.get("truncated"):
                self._need = d.extra["need"]
                # Find the same leader again next time: inside a leader keep from where it was detected; once the
                # frame type is known keep enough leader (KEEP) before it for find_frame.
                back = LEADER_SYM if d.extra.get("in_leader") else self.KEEP
                self._scan = max(self._scan, d.start - back)
                break
            self._scan = d.end
            d.start += self._offset
            d.end += self._offset
            out.append(d)
        if self._scan > 0:                                       # drop what can no longer matter
            self._buf = self._buf[self._scan:]
            self._offset += self._scan
            if self._need:
                self._need -= self._scan
            self._scan = 0
        return out
