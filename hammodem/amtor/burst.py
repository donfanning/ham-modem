"""Burst receiver for the ARQ mode: AMTOR mode A is a sequence of short bursts (a 3-character block of 210 ms or a
1-character control signal of 70 ms) out of silence, so the bit clock comes from the burst itself.

Tone energy is watched in 5 ms hops against a noise floor taken from the quiet hops; a burst's start and end are
located from the energy ramps, its length gives the number of characters, and the bit phase is the one that opens the
eye most around the start. Times are in input samples.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import code, fsk

HOP = 40
KEEP = 4 * fsk.FS
DELAY = 19                         # correlator group delay (samples): d[n] describes input around n - DELAY


@dataclass
class Burst:
    start: int                     # input sample where the burst began
    end: int
    chars: list                    # decoded codes, None where the 4-of-7 check failed
    eye: float

    @property
    def length(self) -> int:
        return self.end - self.start


class BurstReceiver:
    def __init__(self, center: float = fsk.CENTER, shift: float = fsk.SHIFT) -> None:
        self.disc = fsk.Discriminator(center, shift)
        self._d = np.zeros(0)
        self._e = np.zeros(0)
        self._base = 0                  # absolute (output-domain) index of _d[0]
        self._hop = 0                   # next unprocessed hop start, absolute
        self._hops: list[float] = []    # recent hop energies (noise floor estimate)
        self._on = False
        self._low = 0
        self._onset_hop = 0
        self.noise = 0.0

    def feed(self, x: np.ndarray) -> list[Burst]:
        d, e = self.disc.process2(x)
        self._d = np.concatenate((self._d, d))
        self._e = np.concatenate((self._e, e))
        out: list[Burst] = []
        while self._hop + HOP <= self._base + len(self._d):
            lo = self._hop - self._base
            en = float(self._e[lo:lo + HOP].mean())
            thr = self._threshold()
            if not self._on:
                if en > thr and thr > 0:
                    self._on, self._low, self._onset_hop = True, 0, self._hop
                else:
                    self._hops = (self._hops + [en])[-400:]
            else:
                self._low = self._low + 1 if en < thr else 0
                if self._low >= 2:
                    b = self._finish(self._hop - HOP)           # last loud hop started a hop before the first quiet one
                    if b is not None:
                        out.append(b)
                    self._on = False
            self._hop += HOP
        excess = len(self._d) - KEEP
        if excess > 0 and not self._on:
            self._d, self._e = self._d[excess:], self._e[excess:]
            self._base += excess
        return out

    def _threshold(self) -> float:
        if len(self._hops) < 20:
            return 0.0
        floor = float(np.percentile(self._hops, 30))
        self.noise = floor
        return max(floor * 6.0, 1.0)

    def _finish(self, last_loud_hop: int) -> Burst | None:
        e = self._e
        b0 = self._base
        i0 = self._onset_hop - b0
        full = float(np.median(e[i0 + HOP:last_loud_hop - b0 + HOP])) if last_loud_hop - self._onset_hop >= HOP else float(
            e[i0:last_loud_hop - b0 + HOP].max())
        if full <= 0:
            return None
        quarter = 0.25 * full
        rise = i0 - HOP + int(np.argmax(e[max(i0 - HOP, 0):i0 + 2 * HOP] > quarter))
        j = last_loud_hop - b0
        fall = j + int(np.argmax(e[j:j + 2 * HOP] < quarter))
        start, end = b0 + rise - DELAY, b0 + fall - DELAY - 1
        nchars = int(round((end - start) / (7 * fsk.SPB)))
        if nchars < 1 or nchars > 8:
            return None
        nb = 7 * nchars
        cs = np.concatenate(([0.0], np.cumsum(self._d)))
        best, score = 0, -1.0
        for p in range(-16, 17):
            first = start + DELAY - b0 + p
            if first < 0 or first + nb * fsk.SPB >= len(cs):
                continue
            sc = float(np.abs(fsk.bit_sums(cs, first, nb)).sum())
            if sc > score:
                best, score = p, sc
        first = start + DELAY - b0 + best
        if first < 0 or first + nb * fsk.SPB >= len(cs):
            return None
        bits = (fsk.bit_sums(cs, first, nb) > 0).astype(int)
        chars = []
        for k in range(nchars):
            c = code.from_bits(bits[7 * k:7 * k + 7])
            chars.append(c if code.is_valid(c) else None)
        return Burst(start, end, chars, score / nb)
