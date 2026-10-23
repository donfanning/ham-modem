"""DominoEX and Thor: IFK+ (incremental frequency keying) MFSK modes.

Interop target: fldigi.

DominoEX: 18-tone IFK+ at various baud rates, nibble-based varicode,
no FEC (basic mode).  The transmitted tone index is
``(prev_tone + 2 + nibble) mod 18``.

Thor: same IFK+ physical layer but with R=1/2 K=7 convolutional FEC
and a diagonal block interleaver (size 4, depth 10 by default).  Uses
the IZ8BLY MFSK varicode (same as MFSK16).

Reference: fldigi (dominoex.cxx, thor.cxx, dominovar.cxx, thorvaricode.cxx,
           interleave.cxx).
"""

from __future__ import annotations

import numpy as np

from .mfsk16 import (
    POLY1, POLY2, K, N_STATES, _parity,
    ViterbiDecoder, Interleaver, MfskVaricodeDecoder,
    _MFSK_VARICODE, mfsk_varicode_encode,
)

NUMTONES = 18
TWOPI = 2 * np.pi

# ---------------------------------------------------- DominoEX varicode
# Nibble-based.  Nibbles with MSB set (>=8) are continuation nibbles.
# A nibble with MSB clear (0-7) starts a new character.
# Matches fldigi dominovar.cxx verbatim (primary alphabet, chars 0-127).

_DOM_VARICODE: list[list[int]] = [
    [1,15, 9], [1,15,10], [1,15,11], [1,15,12], [1,15,13], [1,15,14], [1,15,15], [2, 8, 8],
    [2,12, 0], [2, 8, 9], [2, 8,10], [2, 8,11], [2, 8,12], [2,13, 0], [2, 8,13], [2, 8,14],
    [2, 8,15], [2, 9, 8], [2, 9, 9], [2, 9,10], [2, 9,11], [2, 9,12], [2, 9,13], [2, 9,14],
    [2, 9,15], [2,10, 8], [2,10, 9], [2,10,10], [2,10,11], [2,10,12], [2,10,13], [2,10,14],
    [0, 0, 0], [7,11, 0], [0, 8,14], [0,10,11], [0, 9,10], [0, 9, 9], [0, 8,15], [7,10, 0],
    [0, 8,12], [0, 8,11], [0, 9,13], [0, 8, 8], [2,11, 0], [7,14, 0], [7,13, 0], [0, 8, 9],
    [3,15, 0], [4,10, 0], [4,15, 0], [5, 9, 0], [6, 8, 0], [5,12, 0], [5,14, 0], [6,12, 0],
    [6,11, 0], [6,14, 0], [0, 8,10], [0, 8,13], [0,10, 8], [7,15, 0], [0, 9,15], [7,12, 0],
    [0, 9, 8], [3, 9, 0], [4,14, 0], [3,12, 0], [3,14, 0], [3, 8, 0], [4,12, 0], [5, 8, 0],
    [5,10, 0], [3,10, 0], [7, 8, 0], [6,10, 0], [4,11, 0], [4, 8, 0], [4,13, 0], [3,11, 0],
    [4, 9, 0], [6,15, 0], [3,13, 0], [2,15, 0], [2,14, 0], [5,11, 0], [6,13, 0], [5,13, 0],
    [5,15, 0], [6, 9, 0], [7, 9, 0], [0,10,14], [0,10, 9], [0,10,15], [0,10,10], [0, 9,12],
    [0, 9,11], [4, 0, 0], [1,11, 0], [0,12, 0], [0,11, 0], [1, 0, 0], [0,15, 0], [1, 9, 0],
    [0,10, 0], [5, 0, 0], [2,10, 0], [1,14, 0], [0, 9, 0], [0,14, 0], [6, 0, 0], [3, 0, 0],
    [1, 8, 0], [2, 8, 0], [7, 0, 0], [0, 8, 0], [2, 0, 0], [0,13, 0], [1,13, 0], [1,12, 0],
    [1,15, 0], [1,10, 0], [2, 9, 0], [0,10,12], [0, 9,14], [0,10,13], [0,11, 8], [2,10,15],
]

_DOM_DECODE: dict[int, int] = {}


def _build_dom_decode() -> None:
    for ch in range(len(_DOM_VARICODE)):
        code = _DOM_VARICODE[ch]
        if code[2] != 0:
            n = 3
        elif code[1] != 0:
            n = 2
        else:
            n = 1
        key = 0
        for i in range(n):
            key |= code[i] << (4 * i)
        _DOM_DECODE[key] = ch


_build_dom_decode()


def dom_varicode_encode(ch: int) -> list[int]:
    if ch < 0 or ch >= len(_DOM_VARICODE):
        return [0]
    code = _DOM_VARICODE[ch]
    if code[2] != 0:
        return list(code)
    if code[1] != 0:
        return code[:2]
    return code[:1]


class DomVaricodeDecoder:
    def __init__(self) -> None:
        self._buf: list[int] = []

    def nibble(self, n: int) -> str:
        if not (n & 0x8):
            ch = self._flush()
            self._buf = [n]
            return ch
        self._buf.append(n)
        if len(self._buf) > 3:
            self._buf = self._buf[-3:]
        return ""

    def _flush(self) -> str:
        if not self._buf:
            return ""
        key = 0
        for i, nib in enumerate(self._buf):
            key |= nib << (4 * i)
        self._buf.clear()
        ch = _DOM_DECODE.get(key)
        if ch is not None and 0 < ch < 128:
            return chr(ch)
        return ""

    def flush(self) -> str:
        return self._flush()


# ---------------------------------------------------- IFK+ modulator

class DominoModulator:
    """Shared IFK+ modulator for DominoEX and Thor."""

    def __init__(self, fs: int = 8000, symlen: int = 1024, doublespaced: int = 2,
                 center: float = 1500.0, level: int = 10000,
                 thor: bool = False, idepth: int = 10) -> None:
        self.fs = fs
        self.symlen = symlen
        self.doublespaced = doublespaced
        self.center = center
        self.level = level
        self.thor = thor
        self.tonespacing = fs * doublespaced / symlen
        self.bandwidth = NUMTONES * self.tonespacing
        self.baud = fs / symlen
        self._txphase = 0.0
        self._txprevtone = 0
        if thor:
            self._encoder_reg = 0
            self._bitstate = 0
            self._bitshreg = 0
            self._interleaver = Interleaver(size=4, depth=idepth, reverse=False)

    def _sendtone(self, tone: int) -> np.ndarray:
        freq = (tone + 0.5) * self.tonespacing + self.center - self.bandwidth / 2.0
        phase_inc = TWOPI * freq / self.fs
        samples = np.empty(self.symlen)
        for i in range(self.symlen):
            samples[i] = np.cos(self._txphase)
            self._txphase -= phase_inc
            if self._txphase < -TWOPI:
                self._txphase += TWOPI
        return np.round(self.level * samples).astype(np.int16)

    def _sendsymbol(self, sym: int) -> np.ndarray:
        tone = (self._txprevtone + 2 + sym) % NUMTONES
        self._txprevtone = tone
        return self._sendtone(tone)

    def _dom_encode_char(self, ch: int) -> np.ndarray:
        nibbles = dom_varicode_encode(ch)
        parts = [self._sendsymbol(n) for n in nibbles]
        return np.concatenate(parts) if parts else np.zeros(0, np.int16)

    def _thor_encode_char(self, ch: int) -> np.ndarray:
        bits_str = _MFSK_VARICODE.get(ch & 0x7F)
        if bits_str is None:
            return np.zeros(0, np.int16)
        bits = [int(b) for b in bits_str]
        parts: list[np.ndarray] = []
        for bit in bits:
            encoded = self._fec_encode(bit)
            for i in range(2):
                self._bitshreg = (self._bitshreg << 1) | ((encoded >> i) & 1)
                self._bitstate += 1
                if self._bitstate == 4:
                    self._bitshreg = self._interleaver.bits(self._bitshreg)
                    parts.append(self._sendsymbol(self._bitshreg & 0xF))
                    self._bitstate = 0
                    self._bitshreg = 0
        return np.concatenate(parts) if parts else np.zeros(0, np.int16)

    def _fec_encode(self, bit: int) -> int:
        self._encoder_reg = ((self._encoder_reg >> 1) | (bit << (K - 2))) & (N_STATES - 1)
        return (_parity(self._encoder_reg & POLY1)) | (_parity(self._encoder_reg & POLY2) << 1)

    def idle(self, seconds: float) -> np.ndarray:
        n_sym = max(1, int(round(seconds * self.baud)))
        if self.thor:
            parts = []
            for _ in range(n_sym):
                parts.append(self._thor_encode_char(0))
            audio = np.concatenate([p for p in parts if len(p)])
            if len(audio) == 0:
                return np.zeros(int(seconds * self.fs), np.int16)
            return audio
        parts = [self._sendsymbol(0) for _ in range(n_sym)]
        return np.concatenate(parts)

    def idle_samples(self, seconds: float) -> np.ndarray:
        return self.idle(seconds)

    def text(self, text: str) -> np.ndarray:
        parts: list[np.ndarray] = []
        for ch in text:
            if self.thor:
                p = self._thor_encode_char(ord(ch))
            else:
                p = self._dom_encode_char(ord(ch))
            if len(p):
                parts.append(p)
        return np.concatenate(parts) if parts else np.zeros(0, np.int16)

    def modulate(self, bits: list[int]) -> np.ndarray:
        return self.text("".join(chr(b) for b in bits))


# ---------------------------------------------------- IFK+ demodulator

class DominoDemodulator:
    """Shared IFK+ demodulator for DominoEX and Thor."""

    def __init__(self, fs: int = 8000, symlen: int = 1024, doublespaced: int = 2,
                 center: float = 1500.0, thor: bool = False, idepth: int = 10) -> None:
        self.fs = fs
        self.symlen = symlen
        self.doublespaced = doublespaced
        self.center = center
        self.thor = thor
        self.tonespacing = fs * doublespaced / symlen
        self.bandwidth = NUMTONES * self.tonespacing
        self.baud = fs / symlen

        basetone_f = center - self.bandwidth / 2.0
        self._tone_freqs = [(i + 0.5) * self.tonespacing + basetone_f for i in range(NUMTONES)]
        n = np.arange(symlen)
        self._basis = np.exp(-2j * np.pi * np.outer(self._tone_freqs, n) / fs)

        self._prev_symbol = 0
        self._decoder = DomVaricodeDecoder()
        if thor:
            self._viterbi = ViterbiDecoder()
            self._interleaver = Interleaver(size=4, depth=idepth, reverse=True)
            self._thor_vardec = MfskVaricodeDecoder()
            self._symbolpair = [0, 0]
            self._symcounter = 0
        self._buf = np.zeros(0, dtype=float)
        self.chars = 0

    def feed(self, x: np.ndarray) -> str:
        x = np.asarray(x, dtype=float)
        self._buf = np.concatenate((self._buf, x))
        text_parts: list[str] = []
        sl = self.symlen
        while len(self._buf) >= sl:
            chunk = self._buf[:sl]
            self._buf = self._buf[sl:]
            energies = np.abs(self._basis @ chunk) ** 2
            best_tone = int(np.argmax(energies))

            nibble = (best_tone - self._prev_symbol - 2) % NUMTONES
            self._prev_symbol = best_tone

            if self.thor:
                ch = self._thor_decode_symbol(nibble)
            else:
                ch = self._decoder.nibble(nibble)
            if ch:
                self.chars += 1
                text_parts.append(ch)
        return "".join(text_parts)

    def _thor_decode_symbol(self, c: int) -> str:
        symbols = [0] * 4
        symbols[3] = 255 if (c & 1) else 0; c //= 2
        symbols[2] = 255 if (c & 1) else 0; c //= 2
        symbols[1] = 255 if (c & 1) else 0; c //= 2
        symbols[0] = 255 if (c & 1) else 0

        symbols = self._interleaver.process(symbols)

        out = ""
        for sym in symbols:
            self._symbolpair[0] = self._symbolpair[1]
            self._symbolpair[1] = sym
            self._symcounter = 0 if self._symcounter else 1
            if self._symcounter:
                continue
            r0 = 1 if self._symbolpair[0] > 127 else 0
            r1 = 1 if self._symbolpair[1] > 127 else 0
            decoded = self._viterbi.step(r0, r1)
            if decoded is not None:
                ch = self._thor_vardec.bit(decoded)
                if ch:
                    out += ch
        return out
