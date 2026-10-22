"""PACTOR-I FSK: 200 Hz shift at 100 or 200 baud, phase-continuous modulator and correlator demodulator.

The AMTOR module runs at 100 bd only; PACTOR needs both 100 and 200, plus soft-output for memory ARQ and per-bit
timing recovery. The discriminator output is the mark-space energy difference (positive = mark = B = 1).
"""

from __future__ import annotations

import numpy as np

FS = 8000
CENTER = 1500.0
SHIFT = 200.0
LEVEL = 10000


def spb(baud: int) -> int:
    return FS // baud


class Modulator:
    def __init__(self, center: float = CENTER, shift: float = SHIFT, level: int = LEVEL) -> None:
        self.mark = center + shift / 2
        self.space = center - shift / 2
        self.level = level
        self._phase = 0.0

    def bits(self, bitvals, baud: int = 100) -> np.ndarray:
        s = spb(baud)
        f = np.where(np.repeat(np.asarray(bitvals, dtype=bool), s), self.mark, self.space)
        ph = self._phase + 2 * np.pi * np.cumsum(f) / FS
        self._phase = float(ph[-1] % (2 * np.pi))
        return np.round(self.level * np.sin(ph)).astype(np.int16)

    def tone(self, freq: float, n_samples: int) -> np.ndarray:
        ph = self._phase + 2 * np.pi * freq * np.arange(n_samples) / FS
        self._phase = float(ph[-1] % (2 * np.pi))
        return np.round(self.level * np.sin(ph)).astype(np.int16)


class Discriminator:
    """Streaming mark-space correlator with soft output."""

    def __init__(self, center: float = CENTER, shift: float = SHIFT, baud: int = 100) -> None:
        self.baud = baud
        self.L = spb(baud) // 2
        self.wm = np.exp(-2j * np.pi * (center + shift / 2) / FS)
        self.ws = np.exp(-2j * np.pi * (center - shift / 2) / FS)
        self._n = 0
        self._hm = np.zeros(self.L - 1, complex)
        self._hs = np.zeros(self.L - 1, complex)
        self._box = np.ones(self.L)

    def process(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Returns (d, energy): discriminator and total tone energy, one sample per input sample."""
        n = self._n + np.arange(len(x))
        self._n += len(x)
        xf = np.asarray(x, float)
        zm = xf * self.wm ** n
        zs = xf * self.ws ** n
        bm = np.concatenate((self._hm, zm))
        bs = np.concatenate((self._hs, zs))
        self._hm, self._hs = bm[len(bm) - self.L + 1:], bs[len(bs) - self.L + 1:]
        m = np.convolve(bm, self._box, "valid")
        s = np.convolve(bs, self._box, "valid")
        pm = m.real ** 2 + m.imag ** 2
        ps = s.real ** 2 + s.imag ** 2
        return (pm - ps) / self.L ** 2, (pm + ps) / self.L ** 2


def slice_bits(disc: np.ndarray, start: int, nbits: int, baud: int = 100) -> tuple[list[int], list[float]]:
    """Hard-slice ``nbits`` from the discriminator starting at sample ``start``.
    Returns (bits, soft_values) where soft > 0 means mark (1)."""
    s = spb(baud)
    bits, soft = [], []
    for i in range(nbits):
        lo = start + i * s
        hi = lo + s
        if hi > len(disc):
            break
        val = float(disc[lo:hi].sum())
        bits.append(1 if val > 0 else 0)
        soft.append(val)
    return bits, soft


def best_phase(disc: np.ndarray, first: int, nbits: int, baud: int = 100) -> tuple[int, float]:
    """Find the bit phase (0..spb-1) that maximises eye opening over ``nbits`` bits."""
    s = spb(baud)
    margin = min(first, s)
    origin = first - margin
    cs = np.concatenate(([0.0], np.cumsum(disc[origin:])))
    best, score = 0, -1.0
    for p in range(s):
        idx = margin + p + s * np.arange(nbits + 1)
        if idx[-1] >= len(cs):
            continue
        e = float(np.abs(np.diff(cs[idx])).sum())
        if e > score:
            best, score = p, e
    return best, score


def detect_burst(energy: np.ndarray, threshold: float = 0.001, min_samples: int = 400) -> int | None:
    """Return the sample index where energy first exceeds ``threshold`` for at least ``min_samples``."""
    above = energy > threshold
    run = 0
    for i, a in enumerate(above):
        if a:
            run += 1
            if run >= min_samples:
                return i - min_samples + 1
        else:
            run = 0
    return None
