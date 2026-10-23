"""MFSK16: 16-tone MFSK at 15.625 baud with R=1/2 K=7 FEC and IZ8BLY varicode.

Interop target: fldigi.

Signal: 16 CPFSK tones spaced 15.625 Hz apart, Gray-coded, with convolutional
FEC (NASA R=1/2 K=7) and a 10-stage diagonal interleaver (IZ8BLY design).
"""

from __future__ import annotations

import numpy as np

# ------------------------------------------------------------------ varicode
# IZ8BLY MFSK varicode (different from PSK31).  Each code includes the
# trailing "00" framing.  The inter-character boundary is detected as "001"
# (two zeros then a one that starts the next character).

_MFSK_VARICODE: dict[int, str] = {
    0: "11101011100", 1: "11101100000", 2: "11101101000", 3: "11101101100",
    4: "11101110000", 5: "11101110100", 6: "11101111000", 7: "11101111100",
    8: "10101000", 9: "11110000000", 10: "11110100000", 11: "11110101000",
    12: "11110101100", 13: "10101100", 14: "11110110000", 15: "11110110100",
    16: "11110111000", 17: "11110111100", 18: "11111000000", 19: "11111010000",
    20: "11111010100", 21: "11111011000", 22: "11111011100", 23: "11111100000",
    24: "11111101000", 25: "11111101100", 26: "11111110000", 27: "11111110100",
    28: "11111111000", 29: "11111111100", 30: "100000000000", 31: "101000000000",
    32: "100", 33: "111000000", 34: "111111100", 35: "1011011000",
    36: "1010101000", 37: "1010100000", 38: "1000000000", 39: "110111100",
    40: "111110100", 41: "111110000", 42: "1010110100", 43: "111100000",
    44: "10100000", 45: "111011000", 46: "111010100", 47: "111101000",
    48: "11100000", 49: "11110000", 50: "101000000", 51: "101010100",
    52: "101110100", 53: "101100000", 54: "101101100", 55: "110100000",
    56: "110000000", 57: "110101100", 58: "111101100", 59: "111111000",
    60: "1011000000", 61: "111011100", 62: "1010111100", 63: "111010000",
    64: "1010000000", 65: "10111100", 66: "100000000", 67: "11010100",
    68: "11011100", 69: "10111000", 70: "11111000", 71: "101010000",
    72: "101011000", 73: "11000000", 74: "110110100", 75: "101111100",
    76: "11110100", 77: "11101000", 78: "11111100", 79: "11010000",
    80: "11101100", 81: "110110000", 82: "11011000", 83: "10110100",
    84: "10110000", 85: "101011100", 86: "110101000", 87: "101101000",
    88: "101110000", 89: "101111000", 90: "110111000", 91: "1011101000",
    92: "1011010000", 93: "1011101100", 94: "1011010100", 95: "1010110000",
    96: "1010101100", 97: "10100", 98: "1100000", 99: "111000",
    100: "110100", 101: "1000", 102: "1010000", 103: "1011000",
    104: "110000", 105: "11000", 106: "10000000", 107: "1110000",
    108: "101100", 109: "1000000", 110: "11100", 111: "10000",
    112: "1010100", 113: "1111000", 114: "100000", 115: "101000",
    116: "1100", 117: "111100", 118: "1101100", 119: "1101000",
    120: "1110100", 121: "1011100", 122: "1111100", 123: "1011011100",
    124: "1010111000", 125: "1011100000", 126: "1011110000", 127: "101010000000",
}

_ENC_MFSK: dict[int, list[int]] = {
    k: [int(b) for b in v] for k, v in _MFSK_VARICODE.items()
}

_DEC_MFSK: dict[tuple[int, ...], int] = {
    tuple(int(b) for b in v): k for k, v in _MFSK_VARICODE.items()
}


def mfsk_varicode_encode(text: str) -> list[int]:
    out: list[int] = []
    for ch in text:
        code = _ENC_MFSK.get(ord(ch) & 0x7F)
        if code is not None:
            out.extend(code)
    return out


class MfskVaricodeDecoder:
    def __init__(self) -> None:
        self._acc: list[int] = []
        self._zeros = 0

    def bit(self, b: int) -> str:
        if b == 0:
            self._zeros += 1
            self._acc.append(0)
            return ""
        if self._zeros >= 2 and self._acc:
            code = tuple(self._acc)
            ch_code = _DEC_MFSK.get(code)
            self._acc = [1]
            self._zeros = 0
            if ch_code is not None:
                return chr(ch_code)
            return ""
        self._zeros = 0
        self._acc.append(1)
        return ""

    def flush(self) -> str:
        if self._acc and self._zeros >= 2:
            for end in range(len(self._acc), 1, -1):
                if self._acc[end - 1] == 0 and self._acc[end - 2] == 0:
                    code = tuple(self._acc[:end])
                    ch_code = _DEC_MFSK.get(code)
                    if ch_code is not None:
                        self._acc.clear()
                        self._zeros = 0
                        return chr(ch_code)
        return ""


# ---------------------------------------------------------- convolutional FEC
# R=1/2 K=7 NASA code: polynomials 0x6D and 0x4F (O1=newest convention)

POLY1 = 0x6D  # 1101101
POLY2 = 0x4F  # 1001111
K = 7
N_STATES = 1 << (K - 1)  # 64


def _parity(x: int) -> int:
    x ^= x >> 4
    x ^= x >> 2
    x ^= x >> 1
    return x & 1


def conv_encode(bits: list[int]) -> list[int]:
    reg = 0
    out: list[int] = []
    for b in bits:
        reg = ((reg >> 1) | (b << (K - 2))) & (N_STATES - 1)
        out.append(_parity(reg & POLY1))
        out.append(_parity(reg & POLY2))
    return out


# Viterbi decoder (hard decision)
_EXPECTED: list[list[tuple[int, int]]] = [[] for _ in range(N_STATES)]
for _s in range(N_STATES):
    for _b in (0, 1):
        _ns = (_s >> 1) | (_b << (K - 2))
        _o1 = _parity(_ns & POLY1)
        _o2 = _parity(_ns & POLY2)
        _EXPECTED[_s].append((_ns, _o1, _o2))

TRACEBACK_DEPTH = 35


class ViterbiDecoder:
    def __init__(self) -> None:
        self._pm = np.full(N_STATES, 1e9)
        self._pm[0] = 0.0
        self._trace: list[np.ndarray] = []
        self._bits: list[int] = []

    def step(self, r0: int, r1: int) -> int | None:
        new_pm = np.full(N_STATES, 1e9)
        pred = np.zeros(N_STATES, dtype=np.int32)
        for s in range(N_STATES):
            for b in (0, 1):
                ns, e0, e1 = _EXPECTED[s][b]
                bm = (r0 ^ e0) + (r1 ^ e1)
                metric = self._pm[s] + bm
                if metric < new_pm[ns]:
                    new_pm[ns] = metric
                    pred[ns] = s
        self._pm = new_pm
        self._pm -= self._pm.min()
        self._trace.append(pred)
        if len(self._trace) >= TRACEBACK_DEPTH:
            return self._traceback()
        return None

    def flush(self) -> list[int]:
        out = []
        while self._trace:
            b = self._traceback()
            if b is not None:
                out.append(b)
        return out

    def _traceback(self) -> int | None:
        if not self._trace:
            return None
        state = int(np.argmin(self._pm))
        for pred in reversed(self._trace[1:]):
            state = int(pred[state])
        bit = (state >> (K - 2)) & 1
        self._trace.pop(0)
        return bit


# ------------------------------------------------------------ interleaver
# Matches fldigi's diagonal interleaver (src/mfsk/interleave.cxx)

INTERLEAVE_DEPTH = 10
INTERLEAVE_SIZE = 4  # bits per symbol


class Interleaver:
    def __init__(self, size: int = INTERLEAVE_SIZE, depth: int = INTERLEAVE_DEPTH,
                 reverse: bool = False) -> None:
        self.size = size
        self.depth = depth
        self.reverse = reverse
        self.table = np.zeros((depth, size, size), dtype=np.uint8)

    def process(self, syms: list[int]) -> list[int]:
        s = list(syms)
        for k in range(self.depth):
            for i in range(self.size):
                for j in range(self.size - 1):
                    self.table[k, i, j] = self.table[k, i, j + 1]
            for i in range(self.size):
                self.table[k, i, self.size - 1] = s[i]
            for i in range(self.size):
                if not self.reverse:
                    s[i] = int(self.table[k, i, self.size - i - 1])
                else:
                    s[i] = int(self.table[k, i, i])
        return s

    def bits(self, val: int) -> int:
        syms = [(val >> (self.size - 1 - i)) & 1 for i in range(self.size)]
        out = self.process(syms)
        result = 0
        for b in out:
            result = (result << 1) | b
        return result


# ---------------------------------------------------------------- Gray code

GRAY_ENC = [0, 1, 3, 2, 6, 7, 5, 4, 12, 13, 15, 14, 10, 11, 9, 8]
GRAY_DEC = [0] * 16
for _i, _g in enumerate(GRAY_ENC):
    GRAY_DEC[_g] = _i


# --------------------------------------------------------------- modulator

class MfskModulator:
    def __init__(self, fs: int = 8000, baud: float = 15.625, n_tones: int = 16,
                 center: float = 1500.0, level: int = 10000) -> None:
        self.fs = fs
        self.baud = baud
        self.n_tones = n_tones
        self.center = center
        self.level = level
        self.spb = int(round(fs / baud))
        spacing = baud
        self.freqs = [center + (i - (n_tones - 1) / 2.0) * spacing for i in range(n_tones)]
        self._phase = 0.0
        self._interleaver = Interleaver()

    def _tone(self, tone_idx: int) -> np.ndarray:
        freq = self.freqs[tone_idx]
        n = self.spb
        t = (np.arange(n) + self._phase) / self.fs
        samples = np.cos(2 * np.pi * freq * t)
        self._phase += n
        return np.round(self.level * samples).astype(np.int16)

    def _encode_symbols(self, fec_bits: list[int]) -> np.ndarray:
        while len(fec_bits) % 4:
            fec_bits.append(0)
        parts = []
        for i in range(0, len(fec_bits), 4):
            nibble = (fec_bits[i] << 3) | (fec_bits[i+1] << 2) | (fec_bits[i+2] << 1) | fec_bits[i+3]
            intlv = self._interleaver.bits(nibble)
            tone = GRAY_ENC[intlv & 0xF]
            parts.append(self._tone(tone))
        return np.concatenate(parts) if parts else np.zeros(0, np.int16)

    def idle(self, seconds: float) -> np.ndarray:
        n_sym = max(1, int(round(seconds * self.baud)))
        n_bits = n_sym * 2  # 2 FEC bits → 1 input bit, 4 FEC bits → 1 symbol
        fec_bits = conv_encode([0] * n_bits)
        return self._encode_symbols(fec_bits)

    def idle_samples(self, seconds: float) -> np.ndarray:
        return self.idle(seconds)

    def text(self, text: str) -> np.ndarray:
        vbits = mfsk_varicode_encode(text)
        fec_bits = conv_encode(vbits)
        return self._encode_symbols(fec_bits)

    def modulate(self, bits: list[int]) -> np.ndarray:
        return self.text("".join(chr(b) for b in bits))


# ------------------------------------------------------------- demodulator

class MfskDemodulator:
    def __init__(self, fs: int = 8000, baud: float = 15.625, n_tones: int = 16,
                 center: float = 1500.0) -> None:
        self.fs = fs
        self.baud = baud
        self.n_tones = n_tones
        self.center = center
        self.spb = int(round(fs / baud))
        spacing = baud
        self.freqs = np.array([center + (i - (n_tones - 1) / 2.0) * spacing
                               for i in range(n_tones)])
        n = np.arange(self.spb)
        self._basis = np.exp(-2j * np.pi * np.outer(self.freqs, n) / fs)
        self._deinterleaver = Interleaver(reverse=True)
        self._viterbi = ViterbiDecoder()
        self._decoder = MfskVaricodeDecoder()
        self._buf = np.zeros(0, dtype=float)
        self.chars = 0
        self._sym_count = 0

    def feed(self, x: np.ndarray) -> str:
        x = np.asarray(x, dtype=float)
        self._buf = np.concatenate((self._buf, x))
        text_parts: list[str] = []
        spb = self.spb
        while len(self._buf) >= spb:
            chunk = self._buf[:spb]
            self._buf = self._buf[spb:]
            self._sym_count += 1
            energies = np.abs(self._basis @ chunk) ** 2
            best_tone = int(np.argmax(energies))
            gray_val = GRAY_DEC[best_tone]
            nibble = self._deinterleaver.bits(gray_val)
            bits = [(nibble >> (3 - j)) & 1 for j in range(4)]
            for j in range(0, 4, 2):
                decoded = self._viterbi.step(bits[j], bits[j + 1])
                if decoded is not None:
                    ch = self._decoder.bit(decoded)
                    if ch:
                        self.chars += 1
                        text_parts.append(ch)
        return "".join(text_parts)
