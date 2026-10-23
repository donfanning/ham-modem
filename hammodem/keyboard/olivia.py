"""Olivia and Contestia: Walsh/Hadamard FEC MFSK modes.

Interop target: fldigi.

Olivia 32/1000 (default): 32 tones, 1000 Hz bandwidth, 31.25 baud,
5 bits/symbol, 7-bit ASCII, 64-symbol blocks carrying 5 characters.

Contestia 32/1000: same physical layer but 6-bit character set (uppercase
only), 32-symbol blocks carrying 5 characters.

FEC: each character is encoded as a Walsh function (via inverse Fast Hadamard
Transform), scrambled with a fixed 64-bit code, and interleaved diagonally
across the block.  The receiver correlates with FHT and picks the peak.

Reference: Pawel Jalocha (SP9VRC), pj_mfsk.h in fldigi.
"""

from __future__ import annotations

import numpy as np

# ------------------------------------------------------------------ constants

SCRAMBLING_OLIVIA = 0xE257E6D0291574EC
SCRAMBLING_CONTESTIA = 0xEDB88320


# ------------------------------------------------------------------- FHT/IFHT

def _fht(data: list[float], length: int) -> None:
    step = 1
    while step < length:
        ptr = 0
        while ptr < length:
            for p in range(ptr, ptr + step):
                a, b = data[p], data[p + step]
                data[p] = b + a
                data[p + step] = b - a
            ptr += 2 * step
        step *= 2


def _ifht(data: list[float], length: int) -> None:
    step = length // 2
    while step >= 1:
        ptr = 0
        while ptr < length:
            for p in range(ptr, ptr + step):
                a, b = data[p], data[p + step]
                data[p] = a - b
                data[p + step] = a + b
            ptr += 2 * step
        step //= 2


# ------------------------------------------------------------ Gray code

def _gray_encode(n: int) -> int:
    return n ^ (n >> 1)


def _gray_decode(g: int) -> int:
    g ^= g >> 16
    g ^= g >> 8
    g ^= g >> 4
    g ^= g >> 2
    g ^= g >> 1
    return g


# --------------------------------------------------------- Olivia encoder

class OliviaEncoder:
    def __init__(self, bits_per_symbol: int = 5, contestia: bool = False) -> None:
        self.bits_per_symbol = bits_per_symbol
        self.contestia = contestia
        self.carriers = 1 << bits_per_symbol
        if contestia:
            self.bits_per_char = 6
            self.scrambling_code = SCRAMBLING_CONTESTIA
            self.n_shift = 5
        else:
            self.bits_per_char = 7
            self.scrambling_code = SCRAMBLING_OLIVIA
            self.n_shift = 13
        self.symbols_per_block = 1 << (self.bits_per_char - 1)

    def _encode_char(self, ch: int) -> list[float]:
        spb = self.symbols_per_block
        mask = (spb << 1) - 1
        if self.contestia:
            ch = _contestia_encode_char(ch)
        else:
            ch &= mask
        buf = [0.0] * spb
        if ch < spb:
            buf[ch] = 1.0
        else:
            buf[ch - spb] = -1.0
        _ifht(buf, spb)
        return buf

    def _scramble(self, buf: list[float], code_offset: int) -> None:
        spb = self.symbols_per_block
        code_wrap = spb - 1
        code_bit = code_offset & code_wrap
        for i in range(spb):
            if self.scrambling_code & (1 << code_bit):
                buf[i] = -buf[i]
            code_bit = (code_bit + 1) & code_wrap

    def encode_block(self, chars: list[int]) -> list[int]:
        """Encode BitsPerSymbol characters into SymbolsPerBlock tone indices."""
        spb = self.symbols_per_block
        bps = self.bits_per_symbol
        output = [0] * spb
        for freq_bit in range(bps):
            buf = self._encode_char(chars[freq_bit] if freq_bit < len(chars) else 0)
            self._scramble(buf, freq_bit * self.n_shift)
            rotate = 0
            for time_bit in range(spb):
                if buf[time_bit] < 0:
                    bit_pos = (freq_bit + rotate) % bps
                    output[time_bit] |= 1 << bit_pos
                rotate = (rotate + 1) % bps
        return output


# --------------------------------------------------------- Olivia decoder

class OliviaDecoder:
    """Matches fldigi's MFSK_SoftDecoder: flat circular buffer, FHT correlation."""

    def __init__(self, bits_per_symbol: int = 5, contestia: bool = False) -> None:
        self.bits_per_symbol = bits_per_symbol
        self.contestia = contestia
        self.carriers = 1 << bits_per_symbol
        if contestia:
            self.bits_per_char = 6
            self.scrambling_code = SCRAMBLING_CONTESTIA
            self.n_shift = 5
        else:
            self.bits_per_char = 7
            self.scrambling_code = SCRAMBLING_OLIVIA
            self.n_shift = 13
        self.symbols_per_block = 1 << (self.bits_per_char - 1)
        self._buf_len = self.symbols_per_block * bits_per_symbol
        self._buf = [0.0] * self._buf_len
        self._inp_ptr = 0
        self._sym_count = 0

    def input_symbol(self, soft_bits: list[float]) -> list[str] | None:
        """Feed one symbol's soft-decision bits.  Returns decoded chars every SymbolsPerBlock symbols."""
        bps = self.bits_per_symbol
        for i in range(bps):
            self._buf[self._inp_ptr] = soft_bits[i]
            self._inp_ptr += 1
        if self._inp_ptr >= self._buf_len:
            self._inp_ptr -= self._buf_len
        self._sym_count += 1
        if self._sym_count >= self.symbols_per_block:
            self._sym_count = 0
            return self._process()
        return None

    def _process(self) -> list[str]:
        bps = self.bits_per_symbol
        spb = self.symbols_per_block
        buf_len = self._buf_len
        chars = []
        for freq_bit in range(bps):
            ptr = self._inp_ptr
            rotate = freq_bit
            code_wrap = spb - 1
            code_bit = (freq_bit * self.n_shift) & code_wrap
            fht_buf = [0.0] * spb
            for time_bit in range(spb):
                idx = ptr + rotate
                if idx >= buf_len:
                    idx -= buf_len
                val = self._buf[idx]
                if self.scrambling_code & (1 << code_bit):
                    val = -val
                fht_buf[time_bit] = val
                code_bit = (code_bit + 1) & code_wrap
                rotate += 1
                if rotate >= bps:
                    rotate -= bps
                ptr += bps
                if ptr >= buf_len:
                    ptr -= buf_len
            _fht(fht_buf, spb)
            peak = 0.0
            peak_pos = 0
            for i in range(spb):
                if abs(fht_buf[i]) > abs(peak):
                    peak = fht_buf[i]
                    peak_pos = i
            ch = peak_pos
            if peak < 0:
                ch += spb
            if self.contestia and ch > 0:
                ch = _contestia_decode_char(ch)
            chars.append(chr(ch) if 0 < ch < 128 else '')
        return chars


# -------------------------------------------------------- Contestia char map

def _contestia_encode_char(ch: int) -> int:
    if isinstance(ch, str):
        ch = ord(ch)
    if ord('a') <= ch <= ord('z'):
        ch = ch - ord('a') + ord('A')
    if ch == ord(' '):
        return 59
    if ch == ord('\r'):
        return 60
    if ch == ord('\n'):
        return 0
    if ch == 8:
        return 61
    if 33 <= ch <= 90:
        return ch - 32
    if ch == 0:
        return 0
    return ord('?') - 32


def _contestia_decode_char(ch: int) -> int:
    if ch == 59:
        return ord(' ')
    if ch == 60:
        return ord('\r')
    if ch == 61:
        return 8
    return ch + 32


# --------------------------------------------------------------- modulator

class OliviaModulator:
    def __init__(self, fs: int = 8000, tones: int = 32, bandwidth: float = 1000.0,
                 center: float = 1500.0, level: int = 10000,
                 contestia: bool = False) -> None:
        self.fs = fs
        self.contestia = contestia
        bits_per_symbol = int(round(np.log2(tones)))
        self.bits_per_symbol = bits_per_symbol
        self.tones = 1 << bits_per_symbol
        self.bandwidth = bandwidth
        self.center = center
        self.level = level
        self.encoder = OliviaEncoder(bits_per_symbol, contestia)

        carrier_separ = 2
        tone_spacing = bandwidth / tones
        symbol_len = int(round(carrier_separ * fs / tone_spacing))
        symbol_len = 1 << int(round(np.log2(symbol_len)))
        self.symbol_len = symbol_len
        self.symbol_separ = symbol_len // 2
        self.baud = fs / self.symbol_separ
        first_carrier = int(round(center * symbol_len / fs)) - carrier_separ * (tones - 1) // 2
        self.first_carrier = first_carrier
        self.carrier_separ = carrier_separ

        self._cos_table = np.cos(2 * np.pi * np.arange(symbol_len) / symbol_len)
        self._out_tap = np.zeros(symbol_len)
        self._tap_ptr = 0
        self._phase = 0
        self._rng = np.random.default_rng(42)

    def _add_symbol(self, freq: int, phase: int) -> None:
        sl = self.symbol_len
        wm = sl - 1
        tp = self._tap_ptr
        for t in range(sl):
            shape = 1.0 - self._cos_table[t]
            self._out_tap[tp] += self._cos_table[phase & wm] * shape
            phase += freq
            tp = (tp + 1) & wm

    def _send_tone(self, symbol: int) -> np.ndarray:
        gray = _gray_encode(symbol)
        freq = self.first_carrier + self.carrier_separ * gray
        sl = self.symbol_len
        ss = self.symbol_separ
        wm = sl - 1

        time_shift = ss // 2 - sl // 2
        self._phase = (self._phase + freq * time_shift) & wm
        self._add_symbol(freq, self._phase)
        time_shift = ss // 2 + sl // 2
        self._phase = (self._phase + freq * time_shift) & wm

        phase_differ = sl // 4 if self._rng.integers(2) else -(sl // 4)
        self._phase = (self._phase + phase_differ) & wm

        out = np.zeros(ss)
        for i in range(ss):
            out[i] = self._out_tap[self._tap_ptr]
            self._out_tap[self._tap_ptr] = 0
            self._tap_ptr = (self._tap_ptr + 1) & wm
        return out

    def _encode_and_send(self, chars: list[int]) -> np.ndarray:
        symbols = self.encoder.encode_block(chars)
        parts = []
        for sym in symbols:
            parts.append(self._send_tone(sym))
        return np.round(self.level * np.concatenate(parts)).astype(np.int16)

    def idle(self, seconds: float) -> np.ndarray:
        bps = self.bits_per_symbol
        spb = self.encoder.symbols_per_block
        n_blocks = max(1, int(round(seconds * self.baud / spb)))
        parts = []
        for _ in range(n_blocks):
            parts.append(self._encode_and_send([0] * bps))
        return np.concatenate(parts)

    def idle_samples(self, seconds: float) -> np.ndarray:
        return self.idle(seconds)

    def text(self, text: str) -> np.ndarray:
        bps = self.bits_per_symbol
        chars = [ord(c) for c in text]
        while len(chars) % bps:
            chars.append(0)
        parts = []
        for i in range(0, len(chars), bps):
            parts.append(self._encode_and_send(chars[i:i + bps]))
        return np.concatenate(parts)

    def modulate(self, bits: list[int]) -> np.ndarray:
        return self.text("".join(chr(b) for b in bits))


# ------------------------------------------------------------- demodulator

class OliviaDemodulator:
    def __init__(self, fs: int = 8000, tones: int = 32, bandwidth: float = 1000.0,
                 center: float = 1500.0, contestia: bool = False) -> None:
        self.fs = fs
        bits_per_symbol = int(round(np.log2(tones)))
        self.bits_per_symbol = bits_per_symbol
        self.tones = 1 << bits_per_symbol
        self.bandwidth = bandwidth
        self.center = center
        self.contestia = contestia

        carrier_separ = 2
        tone_spacing = bandwidth / tones
        symbol_len = int(round(carrier_separ * fs / tone_spacing))
        symbol_len = 1 << int(round(np.log2(symbol_len)))
        self.symbol_len = symbol_len
        self.symbol_separ = symbol_len // 2
        first_carrier = int(round(center * symbol_len / fs)) - carrier_separ * (tones - 1) // 2
        self.first_carrier = first_carrier
        self.carrier_separ = carrier_separ

        cos_table = np.cos(2 * np.pi * np.arange(symbol_len) / symbol_len)
        self._window = (2.0 / symbol_len) * np.sqrt(1.0 - cos_table)

        self._decoder = OliviaDecoder(bits_per_symbol, contestia)
        self._buf = np.zeros(0, dtype=float)
        self._audio_buf = np.zeros(0, dtype=float)
        self.chars = 0

    def _soft_decode(self, block: np.ndarray) -> list[float]:
        """Window a SymbolLen-sized block, FFT, compute soft-decision bits."""
        bps = self.bits_per_symbol
        carriers = self.tones
        cs = self.carrier_separ
        fc = self.first_carrier

        windowed = block * self._window
        spectrum = np.fft.rfft(windowed)
        energies = np.abs(spectrum) ** 2

        carrier_e = np.empty(carriers)
        for idx in range(carriers):
            freq = fc + cs * idx
            carrier_e[idx] = energies[freq] if freq < len(energies) else 0.0

        total_e = np.sum(carrier_e * carrier_e)
        soft = [0.0] * bps
        if total_e <= 0:
            return soft
        for idx in range(carriers):
            sym_idx = _gray_decode(idx)
            e_sq = carrier_e[idx] * carrier_e[idx]
            mask = 1
            for bit in range(bps):
                if sym_idx & mask:
                    soft[bit] -= e_sq / total_e
                else:
                    soft[bit] += e_sq / total_e
                mask <<= 1
        return soft

    def feed(self, x: np.ndarray) -> str:
        x = np.asarray(x, dtype=float)
        self._buf = np.concatenate((self._buf, x))
        text_parts: list[str] = []
        sl = self.symbol_len
        ss = self.symbol_separ
        while len(self._buf) >= ss:
            chunk = self._buf[:ss]
            self._buf = self._buf[ss:]
            self._audio_buf = np.concatenate((self._audio_buf, chunk))
            if len(self._audio_buf) < sl:
                continue
            block = self._audio_buf[-sl:]
            self._audio_buf = self._audio_buf[-(sl - 1):]
            soft = self._soft_decode(block)
            result = self._decoder.input_symbol(soft)
            if result is not None:
                for ch in result:
                    if ch:
                        self.chars += 1
                        text_parts.append(ch)
        return "".join(text_parts)
