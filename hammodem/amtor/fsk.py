"""100 baud FSK for CCIR 476: a phase-continuous two-tone modulator and the receiver front end.

B (mark, 1) is the higher tone, Y (space, 0) the lower; shift 170 Hz about a 1700 Hz audio centre (ITU-R M.476-5
§1.4; some equipment uses 1500 Hz). The front end turns audio into the discriminator d[n] = |mark|^2 - |space|^2
(boxcar correlators of half a bit); a bit is the sign of d summed over its 80 samples at 8 kHz, and the bit phase is
the one that opens the eye most (max over candidate phases of the sum of |bit sums|), which also locates the start
of a burst that begins out of silence.
"""

from __future__ import annotations

import numpy as np

FS = 8000
BAUD = 100
SPB = FS // BAUD                    # samples per bit
CENTER, SHIFT = 1700.0, 170.0
LEVEL = 10000


class Modulator:
    def __init__(self, center: float = CENTER, shift: float = SHIFT, level: int = LEVEL) -> None:
        self.mark, self.space = center + shift / 2, center - shift / 2
        self.level = level
        self._phase = 0.0

    def bits(self, bits) -> np.ndarray:
        """Samples for ``bits`` (1 = B), phase-continuous with the previous call."""
        f = np.where(np.repeat(np.asarray(bits, dtype=bool), SPB), self.mark, self.space)
        ph = self._phase + 2 * np.pi * np.cumsum(f) / FS
        self._phase = float(ph[-1] % (2 * np.pi))
        return np.round(self.level * np.sin(ph)).astype(np.int16)


class Discriminator:
    """Streaming d[n]; output is delayed by half the correlator length (a constant the phase search absorbs)."""

    L = SPB // 2

    def __init__(self, center: float = CENTER, shift: float = SHIFT) -> None:
        self.wm = np.exp(-2j * np.pi * (center + shift / 2) / FS)
        self.ws = np.exp(-2j * np.pi * (center - shift / 2) / FS)
        self._n = 0
        self._hm = np.zeros(self.L - 1, complex)
        self._hs = np.zeros(self.L - 1, complex)
        self._box = np.ones(self.L)

    def process(self, x: np.ndarray) -> np.ndarray:
        return self.process2(x)[0]

    def process2(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(d, e): the discriminator and the total tone energy |mark|^2 + |space|^2 (for burst detection)."""
        n = self._n + np.arange(len(x))
        self._n += len(x)
        zm = np.asarray(x, float) * self.wm ** n
        zs = np.asarray(x, float) * self.ws ** n
        bm = np.concatenate((self._hm, zm))
        bs = np.concatenate((self._hs, zs))
        self._hm, self._hs = bm[len(bm) - self.L + 1:], bs[len(bs) - self.L + 1:]
        m = np.convolve(bm, self._box, "valid")
        s = np.convolve(bs, self._box, "valid")
        pm, ps = m.real ** 2 + m.imag ** 2, s.real ** 2 + s.imag ** 2
        return (pm - ps) / self.L ** 2, (pm + ps) / self.L ** 2


def bit_sums(cs: np.ndarray, start: int, nbits: int) -> np.ndarray:
    """Sum of d over each of ``nbits`` consecutive bit windows from sample ``start`` (cs = cumsum with a leading 0)."""
    idx = start + SPB * np.arange(nbits + 1)
    return np.diff(cs[idx])


def best_phase(cs: np.ndarray, first: int, nbits: int) -> tuple[int, float]:
    """Bit phase (0..SPB-1, relative to ``first``) that opens the eye most over ``nbits`` bits, and its eye sum."""
    best, score = 0, -1.0
    for p in range(SPB):
        e = float(np.abs(bit_sums(cs, first + p, nbits)).sum())
        if e > score:
            best, score = p, e
    return best, score
