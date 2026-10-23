"""RTTY: asynchronous 5-unit ITA2 (Baudot) on two tones. 45.45 baud (the amateur standard), 50, 75 or 100 baud;
shift 170, 425 or 850 Hz; one start bit (space), five data bits LSB first, 1.5 stop bits (mark).

Interop checked against minimodem (lab note). Mark is the higher tone by default (``reverse`` swaps them, the usual
"RTTY reverse" switch).
"""

from __future__ import annotations

import numpy as np

from ..amtor import code as ccir

ITA2_LTRS = ["\0", "E", "\n", "A", " ", "S", "I", "U", "\r", "D", "R", "J", "N", "F", "C", "K",
             "T", "Z", "L", "W", "H", "Y", "P", "Q", "O", "B", "G", "\x0e", "M", "X", "V", "\x0f"]
ITA2_FIGS = ["\0", "3", "\n", "-", " ", "'", "8", "7", "\r", "\x05", "4", "\a", ",", "!", ":", "(",
             "5", "+", ")", "2", "#", "6", "0", "1", "9", "?", "&", "\x0e", ".", "/", "=", "\x0f"]
LTRS_SHIFT, FIGS_SHIFT = 31, 27
_L = {c: i for i, c in enumerate(ITA2_LTRS) if c not in "\0\x0e\x0f"}
_F = {c: i for i, c in enumerate(ITA2_FIGS) if c not in "\0\x0e\x0f"}


def encode_text(text: str, figures: bool = False, usos: bool = True) -> tuple[list[int], bool]:
    """Text -> 5-bit codes with case shifts. ``usos`` (unshift on space) sends LTRS after every space, which
    recovers a receiver that lost a shift."""
    out: list[int] = []
    for ch in text.upper():
        if ch in _L and ch not in _F or (ch in "\r\n " ):
            if figures and ch.isalpha():
                out.append(LTRS_SHIFT)
                figures = False
            out.append(_L[ch])
            if ch == " " and usos and figures:
                out.append(LTRS_SHIFT)
                figures = False
        elif ch in _F:
            if not figures:
                out.append(FIGS_SHIFT)
                figures = True
            out.append(_F[ch])
    return out, figures


class Decoder:
    def __init__(self, usos: bool = True) -> None:
        self.figures = False
        self.usos = usos

    def char(self, c: int) -> str:
        if c == LTRS_SHIFT:
            self.figures = False
            return ""
        if c == FIGS_SHIFT:
            self.figures = True
            return ""
        ch = (ITA2_FIGS if self.figures else ITA2_LTRS)[c]
        if ch == " " and self.usos:
            self.figures = False
        return "" if ch == "\0" else ch


class RttyModulator:
    def __init__(self, fs: int = 8000, baud: float = 45.45, shift: float = 170.0, center: float = 1700.0,
                 reverse: bool = False, level: int = 12000, stop_bits: float = 1.5) -> None:
        self.fs, self.baud, self.level, self.stop = fs, baud, level, stop_bits
        hi, lo = center + shift / 2, center - shift / 2
        self.mark, self.space = (lo, hi) if reverse else (hi, lo)
        self.figures = False
        self._phase = 0.0
        self._carry = 0.0                 # fractional samples carried between bits

    def _bits(self, bits: list[tuple[int, float]]) -> np.ndarray:
        """[(bit, duration in bit periods)] -> samples, phase continuous."""
        parts = []
        for bit, dur in bits:
            n_f = dur * self.fs / self.baud + self._carry
            n = int(round(n_f))
            self._carry = n_f - n
            f = self.mark if bit else self.space
            ph = self._phase + 2 * np.pi * f * np.arange(1, n + 1) / self.fs
            parts.append(np.sin(ph))
            self._phase = float(ph[-1] % (2 * np.pi)) if n else self._phase
        return np.concatenate(parts) if parts else np.zeros(0)

    def idle(self, seconds: float) -> np.ndarray:
        return self._bits([(1, seconds * self.baud)])

    def text(self, text: str) -> np.ndarray:
        codes, self.figures = encode_text(text, self.figures)
        seq: list[tuple[int, float]] = []
        for c in codes:
            seq.append((0, 1.0))
            seq.extend(((c >> i) & 1, 1.0) for i in range(5))
            seq.append((1, self.stop))
        return np.round(self.level * self._bits(seq)).astype(np.int16)

    def idle_samples(self, seconds: float) -> np.ndarray:
        return np.round(self.level * self.idle(seconds)).astype(np.int16)


class RttyDemodulator:
    """Streaming asynchronous decoder. Correlators over a quarter bit give a discriminator; a start bit is the first
    space after mark idle; the five data bits are the sign of the discriminator averaged over the middle half of
    each bit, timed from the start-bit edge; the stop bit must be mark."""

    def __init__(self, fs: int = 8000, baud: float = 45.45, shift: float = 170.0, center: float = 1700.0,
                 reverse: bool = False) -> None:
        self.fs, self.baud = fs, baud
        hi, lo = center + shift / 2, center - shift / 2
        self.mark, self.space = (lo, hi) if reverse else (hi, lo)
        self.spb = fs / baud
        self.L = max(4, int(self.spb / 4))
        self._n = 0
        self._hm = np.zeros(self.L - 1, complex)
        self._hs = np.zeros(self.L - 1, complex)
        self._d = np.zeros(0)              # discriminator, positive = mark
        self._base = 0                     # absolute index of _d[0]
        self._pos = 0                      # absolute index where scanning continues
        self.text = Decoder()
        self.errors = 0
        self.chars = 0
        self.squelch = 0.0

    def _disc(self, x: np.ndarray) -> np.ndarray:
        n = self._n + np.arange(len(x))
        self._n += len(x)
        out = []
        for hist, f in ((self._hm, self.mark), (self._hs, self.space)):
            z = np.asarray(x, float) * np.exp(-2j * np.pi * f * n / self.fs)
            buf = np.concatenate((hist, z))
            hist[:] = buf[len(buf) - self.L + 1:]
            m = np.convolve(buf, np.ones(self.L), "valid")
            out.append(m.real ** 2 + m.imag ** 2)
        return out[0] - out[1]

    def feed(self, x: np.ndarray) -> str:
        self._d = np.concatenate((self._d, self._disc(x)))
        out = []
        spb = self.spb
        delay = self.L / 2
        need = int(spb * 7.5) + 4
        while True:
            i = self._pos - self._base
            if i < 0:
                i = 0
                self._pos = self._base
            seg = self._d[i:]
            if len(seg) < need:
                break
            # start edge: mark -> space
            space = seg[:len(seg) - need + 1] < 0
            prev = np.concatenate(([False], space[:-1]))
            edges = np.flatnonzero(space & ~prev)
            if len(edges) == 0:
                self._advance(len(seg) - need + 1)
                break
            e = int(edges[0])
            if e + need > len(seg):
                self._advance(e)
                break
            # require the first half of the start bit to stay space
            start = e
            ok = np.all(seg[start + int(spb * 0.25):start + int(spb * 0.6)] < 0)
            if not ok:
                self._advance(e + 1)
                continue
            code = 0
            for k in range(5):
                a = start + int(spb * (1.5 + k) - spb * 0.25)
                b = start + int(spb * (1.5 + k) + spb * 0.25)
                if seg[a:b].sum() > 0:
                    code |= 1 << k
            stop_ok = seg[start + int(spb * 6.5 - spb * 0.2):start + int(spb * 6.5 + spb * 0.2)].sum() > 0
            self.chars += 1
            if stop_ok:
                out.append(self.text.char(code))
                self._advance(e + int(spb * 6.6))
            else:
                self.errors += 1
                self._advance(e + int(spb * 0.9))
        keep = len(self._d) - int(spb * 12)
        if self._pos - self._base > 0 and keep > 0 and self._pos - self._base < keep:
            keep = self._pos - self._base
        if keep > 0:
            self._d = self._d[keep:]
            self._base += keep
        return "".join(out)

    def _advance(self, n: int) -> None:
        self._pos += n
