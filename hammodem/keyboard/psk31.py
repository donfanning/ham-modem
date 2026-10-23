"""PSK31 and PSK63: phase-shift keyed keyboard modes.

PSK31 (by G3PLX) uses varicode character encoding and BPSK at 31.25 bd.
PSK63 doubles the rate. Both transmit at a single audio tone (typically
1000 Hz); phase reversals carry the data. A cosine envelope shapes the
transitions to keep the bandwidth narrow.

Interop target: fldigi.
"""

from __future__ import annotations

import numpy as np

# ------------------------------------------------------------------ varicode
# Standard PSK31 varicode — 128 ASCII characters, each as a bit string.
# Bits are transmitted LSB first.  Two consecutive zeros separate characters
# and are NOT part of the character code (every code has at least one 1).

_VARICODE_STRINGS = (
    "1010101011",  # 0x00 NUL
    "1011011011",  # 0x01
    "1011101101",  # 0x02
    "1101110111",  # 0x03
    "1011101011",  # 0x04
    "1101011111",  # 0x05
    "1011101111",  # 0x06
    "1011111101",  # 0x07
    "1011111111",  # 0x08 BS
    "11101111",    # 0x09 TAB
    "11101",       # 0x0A LF
    "1101101111",  # 0x0B
    "1011011101",  # 0x0C
    "11111",       # 0x0D CR
    "1101110101",  # 0x0E
    "1110101011",  # 0x0F
    "1011110111",  # 0x10
    "1011110101",  # 0x11
    "1110101101",  # 0x12
    "1110101111",  # 0x13
    "1101011011",  # 0x14
    "1101101011",  # 0x15
    "1101101101",  # 0x16
    "1101010111",  # 0x17
    "1101111011",  # 0x18
    "1101111101",  # 0x19
    "1110110111",  # 0x1A
    "1101010101",  # 0x1B
    "1101011101",  # 0x1C
    "1110111011",  # 0x1D
    "1011111011",  # 0x1E
    "1101111111",  # 0x1F
    "1",           # 0x20 SPACE
    "111111111",   # 0x21 !
    "101011111",   # 0x22 "
    "111110101",   # 0x23 #
    "111011011",   # 0x24 $
    "1011010101",  # 0x25 %
    "1010111011",  # 0x26 &
    "101111111",   # 0x27 '
    "11111011",    # 0x28 (
    "11110111",    # 0x29 )
    "101101111",   # 0x2A *
    "111011111",   # 0x2B +
    "1110101",     # 0x2C ,
    "110101",      # 0x2D -
    "1010111",     # 0x2E .
    "110101111",   # 0x2F /
    "10110111",    # 0x30 0
    "10111101",    # 0x31 1
    "11101101",    # 0x32 2
    "11111111",    # 0x33 3
    "101110111",   # 0x34 4
    "101011011",   # 0x35 5
    "101101011",   # 0x36 6
    "110101101",   # 0x37 7
    "110101011",   # 0x38 8
    "110110111",   # 0x39 9
    "11110101",    # 0x3A :
    "110111101",   # 0x3B ;
    "111101101",   # 0x3C <
    "1010101",     # 0x3D =
    "111010111",   # 0x3E >
    "1010101111",  # 0x3F ?
    "1010111101",  # 0x40 @
    "1111101",     # 0x41 A
    "11101011",    # 0x42 B
    "10101101",    # 0x43 C
    "10110101",    # 0x44 D
    "1110111",     # 0x45 E
    "11011011",    # 0x46 F
    "11111101",    # 0x47 G
    "101010101",   # 0x48 H
    "1111111",     # 0x49 I
    "111111101",   # 0x4A J
    "101111101",   # 0x4B K
    "11010111",    # 0x4C L
    "10111011",    # 0x4D M
    "11011101",    # 0x4E N
    "10101011",    # 0x4F O
    "11010101",    # 0x50 P
    "111011101",   # 0x51 Q
    "10101111",    # 0x52 R
    "1101111",     # 0x53 S
    "1101101",     # 0x54 T
    "101010111",   # 0x55 U
    "110110101",   # 0x56 V
    "101011101",   # 0x57 W
    "101110101",   # 0x58 X
    "101111011",   # 0x59 Y
    "1010101101",  # 0x5A Z
    "111110111",   # 0x5B [
    "111101111",   # 0x5C backslash
    "111111011",   # 0x5D ]
    "1010111111",  # 0x5E ^
    "101101101",   # 0x5F _
    "1011011111",  # 0x60 `
    "1011",        # 0x61 a
    "1011111",     # 0x62 b
    "101111",      # 0x63 c
    "101101",      # 0x64 d
    "11",          # 0x65 e
    "111101",      # 0x66 f
    "1011011",     # 0x67 g
    "101011",      # 0x68 h
    "1101",        # 0x69 i
    "111101011",   # 0x6A j
    "10111111",    # 0x6B k
    "11011",       # 0x6C l
    "111011",      # 0x6D m
    "1111",        # 0x6E n
    "111",         # 0x6F o
    "111111",      # 0x70 p
    "110111111",   # 0x71 q
    "10101",       # 0x72 r
    "10111",       # 0x73 s
    "101",         # 0x74 t
    "110111",      # 0x75 u
    "1111011",     # 0x76 v
    "1101011",     # 0x77 w
    "11011111",    # 0x78 x
    "1011101",     # 0x79 y
    "111010101",   # 0x7A z
    "1010110111",  # 0x7B {
    "110111011",   # 0x7C |
    "1010110101",  # 0x7D }
    "1011010111",  # 0x7E ~
    "1110110101",  # 0x7F DEL
)

_ENC: dict[int, list[int]] = {}
_TRIE: dict = {}

for _i, _s in enumerate(_VARICODE_STRINGS):
    _ENC[_i] = [int(b) for b in _s]

for _ch, _bits in _ENC.items():
    _node = _TRIE
    for _b in _bits:
        _node = _node.setdefault(_b, {})
    _node["char"] = _ch


def varicode_encode(text: str) -> list[int]:
    """Encode ASCII text to a PSK31 varicode bit stream (with 00 separators)."""
    out: list[int] = []
    for ch in text:
        code = _ENC.get(ord(ch) & 0x7F)
        if code is None:
            continue
        out.extend(code)
        out.extend([0, 0])
    return out


class VaricodeDecoder:
    def __init__(self) -> None:
        self._acc: list[int] = []
        self._zeros = 0

    def bit(self, b: int) -> str:
        if b == 0:
            self._zeros += 1
            if self._zeros >= 2 and self._acc:
                node = _TRIE
                for ab in self._acc:
                    node = node.get(ab, {})
                ch = node.get("char")
                self._acc.clear()
                self._zeros = 0
                if ch is not None:
                    return chr(ch)
            return ""
        if self._zeros == 1:
            self._acc.append(0)
        self._zeros = 0
        self._acc.append(1)
        return ""


# ------------------------------------------------------------------ modulator

class PskModulator:
    """BPSK modulator with raised-cosine shaped transitions."""

    def __init__(self, fs: int = 8000, baud: float = 31.25, center: float = 1000.0,
                 level: int = 10000) -> None:
        self.fs, self.baud, self.center, self.level = fs, baud, center, level
        self.spb = fs / baud
        self._phase = 0.0
        self._cur_sign = 1.0

    def _symbol(self, bit: int) -> np.ndarray:
        """One baud interval.  In BPSK: bit 0 = phase reversal, bit 1 = no change."""
        n = int(round(self.spb))
        t = np.arange(n) / self.fs + self._phase / self.fs
        carrier = np.cos(2 * np.pi * self.center * t)
        if bit == 0:
            ramp = np.linspace(0, np.pi, n)
            env = np.cos(ramp)  # +1 → -1
            samples = self._cur_sign * env * carrier
            self._cur_sign = -self._cur_sign
        else:
            samples = self._cur_sign * carrier
        self._phase += n
        return samples

    def idle(self, seconds: float) -> np.ndarray:
        """Phase reversals (all 0-bits): the standard PSK31 idle pattern."""
        n_symbols = max(1, int(round(seconds * self.baud)))
        return self.modulate([0] * n_symbols)

    def idle_samples(self, seconds: float) -> np.ndarray:
        return self.idle(seconds)

    def modulate(self, bits: list[int]) -> np.ndarray:
        parts = [self._symbol(b) for b in bits]
        if not parts:
            return np.zeros(0, np.int16)
        return np.round(self.level * np.concatenate(parts)).astype(np.int16)

    def text(self, text: str) -> np.ndarray:
        return self.modulate(varicode_encode(text))


# ------------------------------------------------------------------ demodulator

class PskDemodulator:
    """Streaming BPSK demodulator: correlation over each baud, differential phase detection."""

    def __init__(self, fs: int = 8000, baud: float = 31.25, center: float = 1000.0) -> None:
        self.fs, self.baud, self.center = fs, baud, center
        self.spb = fs / baud
        self._n = 0
        self._prev_i = 0.0
        self._prev_q = 0.0
        self._primed = False
        self._decoder = VaricodeDecoder()
        self.chars = 0
        self.errors = 0
        self._buf = np.zeros(0, float)

    def feed(self, x: np.ndarray) -> str:
        x = np.asarray(x, float)
        self._buf = np.concatenate((self._buf, x))
        text = []
        spb = int(round(self.spb))
        half = spb // 2
        while len(self._buf) >= spb:
            chunk = self._buf[:spb]
            self._buf = self._buf[spb:]
            self._n += len(chunk)
            tail = chunk[half:]
            n = np.arange(len(tail)) + self._n - len(tail)
            ci = float(np.sum(tail * np.cos(2 * np.pi * self.center * n / self.fs)))
            cq = float(np.sum(tail * np.sin(2 * np.pi * self.center * n / self.fs)))
            if not self._primed:
                self._prev_i, self._prev_q = ci, cq
                self._primed = True
                continue
            dot = ci * self._prev_i + cq * self._prev_q
            bit = 1 if dot >= 0 else 0
            self._prev_i, self._prev_q = ci, cq
            ch = self._decoder.bit(bit)
            if ch:
                self.chars += 1
                text.append(ch)
        return "".join(text)
