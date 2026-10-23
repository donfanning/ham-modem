"""STANAG 4285 NATO HF serial-tone modem.

Same 8-PSK 2400 baud carrier as MIL-STD-188-110 but with a different frame
structure (256-symbol frames with 80-symbol sync preamble repeated every frame),
a convolutional interleaver (32 rows), and rate-2/3 punctured FEC at 2400 bps.

Key differences from MIL-STD-188-110:
  - Frame: 80 sync + 4×32 data + 3×16 reference = 256 symbols (repeating)
  - Interleaver: convolutional (not block), 32 rows, delay increment k per rate
  - 2400 bps coded: rate-1/2 FEC punctured to rate-2/3
  - Sync sequence: 80-symbol BPSK from x^5+x^2+1 LFSR
  - SOM = 0x03873C3C, EOM = 0x4B65A5B2

Reference: STANAG 4285 (NATO, 1989), METU thesis (Ozkan, 2003).
"""

from __future__ import annotations

import numpy as np

from .milstd import (
    ConvEncoder, ViterbiDecoder,
    _scramble, _descramble,
    _MGD3, _MGD3_INV, _MGD2, _MGD2_INV,
    _rrc, CARRIER_HZ, FS_INTERNAL, SYMBOL_RATE,
)

# ---------------------------------------------------------------------------
# STANAG 4285 constants
# ---------------------------------------------------------------------------

FRAME_SYMBOLS = 256
SYNC_SYMBOLS = 80
DATA_SYMBOLS_PER_FRAME = 128   # 4 × 32
REF_SYMBOLS_PER_FRAME = 48     # 3 × 16
DATA_BLOCK = 32
REF_BLOCK = 16

# Sync sequence generator: x^5 + x^2 + 1, 5-bit LFSR
def _make_sync_sequence() -> np.ndarray:
    state = 0b00001
    out = np.empty(SYNC_SYMBOLS, dtype=np.uint8)
    for i in range(SYNC_SYMBOLS):
        bit = state & 1
        out[i] = bit * 4  # BPSK: 0 -> phase 0, 1 -> phase 4 (180°)
        fb = ((state >> 0) ^ (state >> 2)) & 1
        state = ((state >> 1) | (fb << 4)) & 0x1F
    return out

SYNC_SEQ = _make_sync_sequence()

# Scrambler: same 160-symbol data scrambler as MIL-STD-188-110
from .milstd import DATA_SCRAMBLER, _SCRAMBLER_PERIOD

# SOM/EOM
SOM_BITS = np.array([int(b) for b in f"{0x03873C3C:032b}"], dtype=np.uint8)
EOM_BITS = np.array([int(b) for b in f"{0x4B65A5B2:032b}"], dtype=np.uint8)

# Convolutional interleaver parameters
_CONV_IL_K = {
    2400: {"long": 48, "short": 4},
    1200: {"long": 24, "short": 2},
    600:  {"long": 12, "short": 1},
    300:  {"long": 12, "short": 1},
    150:  {"long": 12, "short": 1},
    75:   {"long": 12, "short": 1},
}

_CONV_IL_ROWS = 32

# Input commutator sequence: (i * 9) mod 32
_INPUT_COMMUTATOR = np.array([(i * 9) % 32 for i in range(32)], dtype=int)

# Output commutator for 2400 bps coded (punctured: skip every 4th row)
_OUTPUT_COMMUTATOR_2400 = np.array([
    0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 13, 14, 16, 17, 18, 20, 21, 22, 24,
    25, 26, 28, 29, 30], dtype=int)

# Normal output: sequential 0..31
_OUTPUT_COMMUTATOR_NORMAL = np.arange(32, dtype=int)

# FEC parameters per rate
_FEC_PARAMS = {
    3600: (3, None, 1),    # (bits_per_sym, effective_rate, repetitions)
    2400: (3, "2/3", 1),   # coded with puncturing
    1200: (2, "1/2", 1),
    600:  (1, "1/2", 1),
    300:  (1, "1/2", 2),
    150:  (1, "1/2", 4),
    75:   (1, "1/2", 8),
}


# ---------------------------------------------------------------------------
# Convolutional interleaver
# ---------------------------------------------------------------------------

class ConvInterleaver:
    """STANAG 4285 convolutional interleaver.

    Standard design: row *r* has a shift register of depth r*k.  The commutator
    visits rows in the non-sequential (i*9) mod 32 order.  Each input symbol is
    pushed into the register for the current row and the oldest symbol is popped
    out, so the same commutator drives both write and read.
    """

    def __init__(self, k: int):
        self._k = k
        self._rows = _CONV_IL_ROWS
        self._regs: list[list[int]] = []
        for r in range(self._rows):
            self._regs.append([0] * (r * k))
        self._pos = 0

    def interleave(self, bits: np.ndarray) -> np.ndarray:
        out = []
        for b in bits:
            row = _INPUT_COMMUTATOR[self._pos % self._rows]
            self._regs[row].append(int(b))
            out.append(self._regs[row].pop(0))
            self._pos += 1
        return np.array(out, dtype=np.uint8)

    def flush(self, count: int) -> np.ndarray:
        return self.interleave(np.zeros(count, dtype=np.uint8))


class ConvDeinterleaver:
    """Inverse of ConvInterleaver — complementary delays (31-r)*k."""

    def __init__(self, k: int):
        self._k = k
        self._rows = _CONV_IL_ROWS
        max_row = self._rows - 1
        self._regs: list[list[float]] = []
        for r in range(self._rows):
            self._regs.append([0.0] * ((max_row - r) * k))
        self._pos = 0

    def deinterleave(self, soft: np.ndarray) -> np.ndarray:
        out = []
        for s in soft:
            row = _INPUT_COMMUTATOR[self._pos % self._rows]
            self._regs[row].append(float(s))
            out.append(self._regs[row].pop(0))
            self._pos += 1
        return np.array(out)


# ---------------------------------------------------------------------------
# Modulator
# ---------------------------------------------------------------------------

class Stanag4285Modulator:
    """STANAG 4285 8-PSK serial-tone modulator."""

    def __init__(self, fs: int = 8000, rate: int = 2400, interleave: str = "short",
                 center: float = CARRIER_HZ):
        self.fs = fs
        self.center = center
        self.rate = rate
        self.interleave = interleave
        bps, fec, reps = _FEC_PARAMS[rate]
        self.bits_per_sym = bps
        self.coded = fec is not None
        self.repetitions = reps
        self.puncture_2400 = (rate == 2400 and self.coded)
        self._sps = FS_INTERNAL // SYMBOL_RATE
        self._rrc = _rrc(self._sps, alpha=0.35, span=8)
        self._encoder = ConvEncoder() if self.coded else None
        if self.coded and interleave != "zero":
            k = _CONV_IL_K[rate][interleave]
            self._il = ConvInterleaver(k)
        else:
            self._il = None

    def _tribits_to_phasors(self, tribits: np.ndarray) -> np.ndarray:
        return np.exp(1j * np.pi / 4 * tribits.astype(float))

    def _pulse_shape(self, phasors: np.ndarray) -> np.ndarray:
        impulses = np.zeros(len(phasors) * self._sps, dtype=complex)
        impulses[::self._sps] = phasors
        return np.convolve(impulses, self._rrc, mode="same")

    def _to_audio(self, baseband: np.ndarray) -> np.ndarray:
        n = np.arange(len(baseband))
        carrier = np.exp(2j * np.pi * self.center * n / FS_INTERNAL)
        passband = np.real(baseband * carrier)
        if self.fs == FS_INTERNAL:
            return (passband * 16000).astype(np.int16)
        ratio = self.fs / FS_INTERNAL
        out_len = int(len(passband) * ratio)
        indices = np.arange(out_len) / ratio
        idx = indices.astype(int)
        frac = indices - idx
        idx = np.clip(idx, 0, len(passband) - 2)
        resampled = passband[idx] * (1 - frac) + passband[idx + 1] * frac
        return (resampled * 16000).astype(np.int16)

    def _encode_data(self, user_bits: np.ndarray) -> np.ndarray:
        bits = np.asarray(user_bits, dtype=np.uint8)
        if self._encoder is not None:
            coded = self._encoder.encode(bits)
            if self.repetitions > 1:
                pairs = coded.reshape(-1, 2)
                coded = np.repeat(pairs, self.repetitions, axis=0).reshape(-1)
        else:
            coded = bits
        if self._il is not None:
            coded = self._il.interleave(coded)
        return coded

    def _bits_to_data_tribits(self, channel_bits: np.ndarray) -> np.ndarray:
        bps = self.bits_per_sym
        n_syms = len(channel_bits) // bps
        bits = channel_bits[:n_syms * bps].reshape(-1, bps)
        if bps == 3:
            vals = (bits[:, 0] << 2) | (bits[:, 1] << 1) | bits[:, 2]
            return _MGD3[vals]
        elif bps == 2:
            vals = (bits[:, 0] << 1) | bits[:, 1]
            return np.array([0, 2, 4, 6], dtype=np.uint8)[_MGD2[vals]]
        else:
            return np.where(bits.flatten(), np.uint8(4), np.uint8(0))

    def _make_frames(self, data_tribits: np.ndarray) -> np.ndarray:
        """Build 256-symbol frames: 80 sync + (32 data + 16 ref) × interleaved."""
        frames = []
        pos = 0
        ref = np.zeros(REF_BLOCK, dtype=np.uint8)
        while pos < len(data_tribits):
            frame = [SYNC_SEQ.copy()]
            for seg in range(4):
                chunk = data_tribits[pos:pos + DATA_BLOCK]
                if len(chunk) < DATA_BLOCK:
                    chunk = np.concatenate([chunk, np.zeros(DATA_BLOCK - len(chunk), dtype=np.uint8)])
                frame.append(chunk)
                if seg < 3:
                    frame.append(ref.copy())
                pos += DATA_BLOCK
            frames.append(np.concatenate(frame))
        return np.concatenate(frames) if frames else np.zeros(0, dtype=np.uint8)

    def transmit(self, data: bytes) -> np.ndarray:
        user_bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8), bitorder="big")
        all_bits = np.concatenate([SOM_BITS, user_bits, EOM_BITS])
        if self.coded:
            flush_k = _CONV_IL_K.get(self.rate, {}).get(self.interleave, 0)
            il_flush = (_CONV_IL_ROWS - 1) * flush_k * _CONV_IL_ROWS // 2 if flush_k else 0
            flush_count = il_flush + 10
            all_bits = np.concatenate([all_bits, np.zeros(flush_count, dtype=np.uint8)])
        channel_bits = self._encode_data(all_bits)
        data_tribits = self._bits_to_data_tribits(channel_bits)
        framed = self._make_frames(data_tribits)
        preamble = self._make_frames(np.zeros(DATA_SYMBOLS_PER_FRAME, dtype=np.uint8))
        framed = np.concatenate([preamble, framed, preamble])
        scr_len = DATA_SYMBOLS_PER_FRAME + REF_SYMBOLS_PER_FRAME
        n_frames = len(framed) // FRAME_SYMBOLS
        scrambled = np.copy(framed)
        scr_pos = 0
        total_scr = n_frames * scr_len
        scr_seq = np.tile(DATA_SCRAMBLER, (total_scr + scr_len + _SCRAMBLER_PERIOD - 1) // _SCRAMBLER_PERIOD)
        for i in range(n_frames):
            base = i * FRAME_SYMBOLS + SYNC_SYMBOLS
            block = framed[base:base + scr_len]
            scrambled[base:base + scr_len] = _scramble(block, scr_seq[scr_pos:scr_pos + len(block)])
            scr_pos += len(block)
        phasors = self._tribits_to_phasors(scrambled)
        baseband = self._pulse_shape(phasors)
        return self._to_audio(baseband)

    def idle(self, duration: float) -> np.ndarray:
        return np.zeros(int(self.fs * duration), np.int16)


# ---------------------------------------------------------------------------
# Demodulator
# ---------------------------------------------------------------------------

class Stanag4285Demodulator:
    """STANAG 4285 8-PSK serial-tone demodulator."""

    def __init__(self, fs: int = 8000, rate: int = 2400, interleave: str = "short",
                 center: float = CARRIER_HZ):
        self.fs = fs
        self.center = center
        self.rate = rate
        self.interleave = interleave
        bps, fec, reps = _FEC_PARAMS[rate]
        self.bits_per_sym = bps
        self.coded = fec is not None
        self.repetitions = reps
        self.puncture_2400 = (rate == 2400 and self.coded)
        self._sps = FS_INTERNAL // SYMBOL_RATE
        self._rrc = _rrc(self._sps, alpha=0.35, span=8)
        if self.coded and interleave != "zero":
            k = _CONV_IL_K[rate][interleave]
            self._deil = ConvDeinterleaver(k)
        else:
            self._deil = None

    def _resample_to_internal(self, audio: np.ndarray) -> np.ndarray:
        if self.fs == FS_INTERNAL:
            return audio.astype(float)
        ratio = FS_INTERNAL / self.fs
        out_len = int(len(audio) * ratio)
        indices = np.arange(out_len) / ratio
        idx = np.clip(indices.astype(int), 0, len(audio) - 2)
        frac = indices - idx
        return audio[idx].astype(float) * (1 - frac) + audio[idx + 1].astype(float) * frac

    def _to_baseband(self, x: np.ndarray) -> np.ndarray:
        n = np.arange(len(x))
        return x * np.exp(-2j * np.pi * self.center * n / FS_INTERNAL)

    def _matched_filter(self, baseband: np.ndarray) -> np.ndarray:
        return np.convolve(baseband, self._rrc, mode="same")

    def _find_sync(self, filtered: np.ndarray) -> int | None:
        """Find the first 256-symbol frame by correlating with the 80-symbol sync."""
        sps = self._sps
        sync_phasors = np.exp(1j * np.pi / 4 * SYNC_SEQ.astype(float))
        tpl = sync_phasors[:40]
        tpl_norm = np.linalg.norm(tpl)
        best_corr, best_off = 0.0, 0
        n_syms = len(filtered) // sps
        if n_syms < SYNC_SYMBOLS:
            return None
        for off in range(sps):
            syms = filtered[off::sps]
            if len(syms) < SYNC_SYMBOLS:
                continue
            search_end = min(len(syms) - len(tpl) + 1, n_syms)
            corr = np.abs(np.correlate(syms[:search_end + len(tpl)], tpl, mode="valid"))
            for i in range(min(len(corr), search_end)):
                seg_norm = np.linalg.norm(syms[i:i + len(tpl)])
                if seg_norm < 1e-12:
                    continue
                c = corr[i] / (seg_norm * tpl_norm)
                if c > 0.8:
                    sample_off = off + i * sps
                    if best_corr < 0.5 or sample_off < best_off:
                        best_corr = c
                        best_off = sample_off
                    break
        return best_off if best_corr > 0.5 else None

    def _extract_frames(self, filtered: np.ndarray, start: int) -> list[np.ndarray]:
        """Extract data tribits from consecutive 256-symbol frames."""
        sps = self._sps
        frames_data = []
        pos = start
        while pos + FRAME_SYMBOLS * sps <= len(filtered):
            syms = filtered[pos::sps][:FRAME_SYMBOLS]
            if len(syms) < FRAME_SYMBOLS:
                break
            phases = np.angle(syms)
            tribits = np.round(phases / (np.pi / 4)).astype(int) % 8
            tribits = tribits.astype(np.uint8)
            data_block = tribits[SYNC_SYMBOLS:]
            scr_len = DATA_SYMBOLS_PER_FRAME + REF_SYMBOLS_PER_FRAME
            scr_pos = len(frames_data) * scr_len
            scr_needed = scr_pos + scr_len
            scr_seq = np.tile(DATA_SCRAMBLER, (scr_needed + _SCRAMBLER_PERIOD - 1) // _SCRAMBLER_PERIOD)
            descrambled = _descramble(data_block[:scr_len], scr_seq[scr_pos:scr_pos + scr_len])
            data_tribits = []
            p = 0
            for seg in range(4):
                data_tribits.append(descrambled[p:p + DATA_BLOCK])
                p += DATA_BLOCK
                if seg < 3:
                    p += REF_BLOCK
            frames_data.append(np.concatenate(data_tribits))
            pos += FRAME_SYMBOLS * sps
        return frames_data

    def _tribits_to_soft(self, tribits: np.ndarray) -> np.ndarray:
        bps = self.bits_per_sym
        if bps == 3:
            inv = _MGD3_INV[tribits]
            bits = np.empty(len(inv) * 3, dtype=np.uint8)
            bits[0::3] = (inv >> 2) & 1
            bits[1::3] = (inv >> 1) & 1
            bits[2::3] = inv & 1
        elif bps == 2:
            mapped = np.zeros(len(tribits), dtype=np.uint8)
            mapped[tribits == 0] = 0
            mapped[tribits == 2] = 1
            mapped[tribits == 4] = 2
            mapped[tribits == 6] = 3
            inv = _MGD2_INV[mapped]
            bits = np.empty(len(inv) * 2, dtype=np.uint8)
            bits[0::2] = (inv >> 1) & 1
            bits[1::2] = inv & 1
        else:
            bits = (tribits >= 4).astype(np.uint8)
        return 1.0 - 2.0 * bits.astype(float)

    def feed(self, audio: np.ndarray) -> bytes:
        x = self._resample_to_internal(np.asarray(audio, dtype=float))
        bb = self._to_baseband(x)
        filt = self._matched_filter(bb)
        start = self._find_sync(filt)
        if start is None:
            return b""
        frames_data = self._extract_frames(filt, start)
        if not frames_data:
            return b""
        all_data = np.concatenate(frames_data)
        soft = self._tribits_to_soft(all_data)
        if self._deil is not None:
            soft = self._deil.deinterleave(soft)
        if self.repetitions > 1:
            r = self.repetitions
            usable = (len(soft) // (2 * r)) * (2 * r)
            soft = soft[:usable].reshape(-1, r, 2).sum(axis=1).reshape(-1)
        if self.coded:
            vit = ViterbiDecoder()
            bits = np.concatenate([vit.decode(soft), vit.flush()])
        else:
            bits = (soft < 0).astype(np.uint8)
        eom_soft = 1.0 - 2.0 * EOM_BITS.astype(float)
        som_soft = 1.0 - 2.0 * SOM_BITS.astype(float)
        bit_soft = 1.0 - 2.0 * bits.astype(float)
        som_corr = np.correlate(bit_soft, som_soft, mode="valid")
        som_pos = None
        if len(som_corr) > 0:
            best_som = int(np.argmax(som_corr))
            if som_corr[best_som] > 20:
                som_pos = best_som + 32
        eom_corr = np.correlate(bit_soft, eom_soft, mode="valid")
        eom_pos = None
        if len(eom_corr) > 0:
            best_eom = int(np.argmax(eom_corr))
            if eom_corr[best_eom] > 20:
                eom_pos = best_eom
        if som_pos is not None and eom_pos is not None and eom_pos > som_pos:
            bits = bits[som_pos:eom_pos]
        elif eom_pos is not None:
            bits = bits[:eom_pos]
        elif som_pos is not None:
            bits = bits[som_pos:]
        pad = (-len(bits)) % 8
        if pad:
            bits = np.concatenate([bits, np.zeros(pad, dtype=np.uint8)])
        out = np.packbits(bits.reshape(-1, 8), bitorder="big", axis=1).flatten()
        return bytes(out)
