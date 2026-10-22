"""ARDOP channel-busy detector (port of ardopcf's BusyDetect3): every 100 ms while idle, a Blackman-Harris windowed
1024-point FFT of the last 12 kHz samples; the strongest narrow (8 bins, ~94 Hz) and wide (2/3 of the passband) groups
of bins are compared with the rest of the passband. Busy needs 3 consecutive hits, then holds for 5 s.

ardopcf (MIT, Muething, Wiseman, LaRue) keeps its "slow average" in function-local variables, so the average is in
effect 0.2 x the current ratio except right after a passband change; that is reproduced so BUSYDET means the same.
"""

from __future__ import annotations

import numpy as np

CHECK_MS = 100
HOLD_MS = 5000
BIN_HZ = 12000 / 1024
ALPHA = 0.2
_n = np.arange(1024)
_WINDOW = (0.35875 - 0.48829 * np.cos(2 * np.pi * _n / 1023) + 0.14128 * np.cos(4 * np.pi * _n / 1023)
           - 0.01168 * np.cos(6 * np.pi * _n / 1023))


def _split(mag: np.ndarray, lo: int, hi: int, top: int) -> float:
    """ardopcf SortSignals2: mean of the ``top`` strongest bins over the mean of the others (its divisor included)."""
    s = np.sort(mag[lo:hi + 1])
    base = s[:-top].sum() / (hi - lo - top - 1)
    return (s[-top:].sum() / top) / base if base > 0 else 0.0           # digital silence is not a signal


class BusyDetector:
    def __init__(self, level: int = 5) -> None:
        self.level = level                       # BUSYDET 0 (off) .. 10
        self.busy = False
        self.last_trip = self.prior_trip = -1e9  # ms of the last / previous busy trip
        self.last_clear = 0.0
        self._on = self._off = 0
        self._last_hit = 0.0
        self._range: tuple[int, int] | None = None

    def clear(self, now: float) -> None:
        """ardopcf ClearBusy."""
        self.last_trip = self.prior_trip = now
        self.last_clear = now + 610
        self._last_hit = now - HOLD_MS
        self.busy = False
        self._on = self._off = 0
        self._range = None

    def update(self, samples: np.ndarray, now: float, bandwidth: int, tuning_range: int = 100) -> bool:
        if len(samples) < 1024:
            return self.busy
        spec = np.fft.rfft(samples[-1024:] * _WINDOW)
        mag = (spec.real ** 2 + spec.imag ** 2)[25:25 + 206]
        delta = int((bandwidth / 2 + tuning_range) / BIN_HZ)
        lo, hi = max(103 - delta, 3), min(103 + delta, 203)
        narrow, wide = _split(mag, lo, hi, 8), _split(mag, lo, hi, ((hi - lo) * 2) // 3)
        if self._range == (lo, hi):
            narrow, wide = ALPHA * narrow, ALPHA * wide
        self._range = (lo, hi)
        wide_k = 0.02 if bandwidth <= 500 else 0.016
        hit = self.level > 0 and (narrow > 3 + 0.008 * self.level ** 4 or wide > 5 + wide_k * self.level ** 4)
        if hit:
            self._on, self._off = self._on + 1, 0
            if self._on > 3:
                self._last_hit = now
        else:
            self._on, self._off = 0, self._off + 1
        if not self.busy and self._on >= 3:
            self.prior_trip, self.last_trip = self.last_trip, now
            self.busy = True
        elif self.busy and now - self._last_hit > HOLD_MS and self._off >= 3:
            self.last_clear = now
            self.busy = False
        return self.busy

    def blocks_connection(self, leader_detect: float) -> bool:
        """ardopcf BUSYBLOCK test for an incoming connection request: it was heard while the channel was busy, or
        within 600 ms of the busy detector clearing."""
        if leader_detect - self.last_trip < 300:            # the request's own leader tripped the detector
            return self.last_clear - self.prior_trip < 600
        return self.last_clear - self.last_trip < 600
