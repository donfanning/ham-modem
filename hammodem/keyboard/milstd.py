"""MIL-STD-188-110 / STANAG 4285 serial-tone HF modem.

Physical layer: 8-PSK on a 1800 Hz carrier at 2400 baud, pulse-shaped with a
root-raised-cosine filter (roll-off 0.35 for telephone-bandwidth compatibility).
Data rates 75–4800 bps via a K=7 rate-1/2 convolutional FEC, Modified Gray
mapping, block interleaver (short/long), and a 160-symbol scrambler.

Reference: MIL-STD-188-110C §5.3.2 (serial tone), transcribed constants from
the MIT-licensed m110c project (github.com/Willt125/MIL-STD-188-110).
"""

from __future__ import annotations

import numpy as np

# ---------------------------------------------------------------------------
# Constants from MIL-STD-188-110C §5.3.2
# ---------------------------------------------------------------------------

SYMBOL_RATE = 2400
CARRIER_HZ = 1800
FS_INTERNAL = 9600  # modulation sample rate (integer sps = 4)

# FEC: K=7 rate-1/2 convolutional code (Figure 4)
# T1: x^6 + x^4 + x^3 + x + 1  -> octal 133
# T2: x^6 + x^5 + x^4 + x^3 + 1 -> octal 171
_POLY_T1 = 0o133  # 91 decimal
_POLY_T2 = 0o171  # 121 decimal
K_CONSTRAINT = 7
NUM_STATES = 64

# Scrambler: 12-bit Galois LFSR, polynomial x^12+x^6+x^4+x+1, init 0xBAD
_SCRAMBLER_INIT = 0xBAD
_SCRAMBLER_FEEDBACK = (1 << 0) | (1 << 1) | (1 << 4) | (1 << 6)  # cells 0,1,4,6
_SCRAMBLER_PERIOD = 160

# Sync scrambler: fixed 32-symbol sequence (§5.3.2.3.8.2)
SYNC_SCRAMBLER = np.array([7, 4, 3, 0, 5, 1, 5, 0, 2, 2, 1, 1, 5, 7, 4, 3,
                           5, 0, 2, 6, 2, 1, 6, 2, 0, 0, 5, 0, 5, 2, 6, 6], dtype=np.uint8)

# EOM sequence (§5.3.2.3.1): 0x4B65A5B2, MSB first
EOM_BITS = np.array([int(b) for b in f"{0x4B65A5B2:032b}"], dtype=np.uint8)

# Modified Gray tables (§5.3.2.3.6)
_MGD3 = np.array([0, 1, 3, 2, 7, 6, 4, 5], dtype=np.uint8)  # Table VIII: 3-bit
_MGD3_INV = np.argsort(_MGD3).astype(np.uint8)
_MGD2 = np.array([0, 1, 3, 2], dtype=np.uint8)  # Table IX: 2-bit
_MGD2_INV = np.argsort(_MGD2).astype(np.uint8)

# Figure 5: channel symbol placement on 8-PSK circle
_MAP_1BIT = np.array([0, 4], dtype=np.uint8)
_MAP_2BIT = np.array([0, 2, 4, 6], dtype=np.uint8)

# Table XIII: preamble channel symbol -> 32-tribit pattern
_TABLE_XIII = np.array([
    [0, 0, 0, 0, 0, 0, 0, 0] * 4,
    [0, 4, 0, 4, 0, 4, 0, 4] * 4,
    [0, 0, 4, 4, 0, 0, 4, 4] * 4,
    [0, 4, 4, 0, 0, 4, 4, 0] * 4,
    [0, 0, 0, 0, 4, 4, 4, 4] * 4,
    [0, 4, 0, 4, 4, 0, 4, 0] * 4,
    [0, 0, 4, 4, 4, 4, 0, 0] * 4,
    [0, 4, 4, 0, 4, 0, 0, 4] * 4,
], dtype=np.uint8)

# Table XI: D1/D2 designation symbols (rate, interleave) -> (D1, D2)
_TABLE_XI = {
    (4800, "short"): (7, 6), (2400, "short"): (6, 4), (2400, "long"): (4, 4),
    (1200, "short"): (6, 5), (1200, "long"): (4, 5),
    (600, "short"): (6, 6), (600, "long"): (4, 6),
    (300, "short"): (6, 7), (300, "long"): (4, 7),
    (150, "short"): (7, 4), (150, "long"): (5, 4),
    (75, "short"): (7, 5), (75, "long"): (5, 5),
}

# Preamble structure (§5.3.2.3.7.2.1)
_PREAMBLE_PREFIX = (0, 1, 3, 0, 1, 3, 1, 2, 0)
_PREAMBLE_SUFFIX = (0,)
_PREAMBLE_SEGMENTS = {"short": 3, "long": 24}
_PREAMBLE_COUNT_START = {"short": 2, "long": 23}

# Frame layout (Table XV)
_FRAME_LAYOUT = {
    4800: (3, 1, 32, 16, None),   # bits_per_sym, tribits_per_sym, unknown, known, coding_rate
    2400: (3, 1, 32, 16, 0.5),
    1200: (2, 1, 20, 20, 0.5),
    600:  (1, 1, 20, 20, 0.5),
    300:  (1, 1, 20, 20, 0.25),
    150:  (1, 1, 20, 20, 0.125),
    75:   (2, 32, None, 0, 0.5),
}

# Repetitions (Table V)
_REPETITIONS = {4800: 1, 2400: 1, 1200: 1, 600: 1, 300: 2, 150: 4, 75: 1}

# Interleaver dimensions (Table VI)
_INTERLEAVER = {
    2400: {"long": (40, 576), "short": (40, 72)},
    1200: {"long": (40, 288), "short": (40, 36)},
    600:  {"long": (40, 144), "short": (40, 18)},
    300:  {"long": (40, 144), "short": (40, 18)},
    150:  {"long": (40, 144), "short": (40, 18)},
    75:   {"long": (20, 36), "short": (10, 9)},
}


# ---------------------------------------------------------------------------
# Scrambler
# ---------------------------------------------------------------------------

def _make_data_scrambler() -> np.ndarray:
    """One period (160 symbols) of the data scrambling sequence."""
    out = np.empty(_SCRAMBLER_PERIOD, dtype=np.uint8)
    state = _SCRAMBLER_INIT
    mask = 0xFFF
    for i in range(_SCRAMBLER_PERIOD):
        for _ in range(8):
            leaving = (state >> 11) & 1
            state = (state << 1) & mask
            if leaving:
                state ^= _SCRAMBLER_FEEDBACK
        out[i] = ((state >> 2) & 1) << 2 | ((state >> 1) & 1) << 1 | (state & 1)
    return out


DATA_SCRAMBLER = _make_data_scrambler()


def _scramble(tribits: np.ndarray, seq: np.ndarray) -> np.ndarray:
    return ((tribits.astype(int) + seq.astype(int)) % 8).astype(np.uint8)


def _descramble(tribits: np.ndarray, seq: np.ndarray) -> np.ndarray:
    return ((tribits.astype(int) - seq.astype(int)) % 8).astype(np.uint8)


# ---------------------------------------------------------------------------
# FEC: transition table and K=7 rate-1/2 convolutional encoder
# ---------------------------------------------------------------------------

def _build_transitions():
    """Build transition table: (prev_state, input) -> (next_state, t1, t2).
    State convention: bit 5 = delay 1 (newest), bit 0 = delay 6 (oldest)."""
    nxt = np.zeros((NUM_STATES, 2), dtype=int)
    out1 = np.zeros((NUM_STATES, 2), dtype=np.uint8)
    out2 = np.zeros((NUM_STATES, 2), dtype=np.uint8)
    for s in range(NUM_STATES):
        for u in range(2):
            word = u
            for i in range(6):
                word |= ((s >> (5 - i)) & 1) << (i + 1)
            nxt[s, u] = (u << 5) | (s >> 1)
            out1[s, u] = bin(word & _POLY_T1).count('1') % 2
            out2[s, u] = bin(word & _POLY_T2).count('1') % 2
    return nxt, out1, out2


_NEXT_STATE, _OUT_T1, _OUT_T2 = _build_transitions()


class ConvEncoder:
    def __init__(self):
        self._state = 0

    def encode(self, bits: np.ndarray) -> np.ndarray:
        bits = np.asarray(bits, dtype=np.uint8)
        out = np.empty(2 * bits.size, dtype=np.uint8)
        s = self._state
        for i, u in enumerate(bits):
            out[2 * i] = _OUT_T1[s, u]
            out[2 * i + 1] = _OUT_T2[s, u]
            s = _NEXT_STATE[s, u]
        self._state = s
        return out


# ---------------------------------------------------------------------------
# Viterbi decoder (soft-decision, streaming)
# ---------------------------------------------------------------------------

def _build_viterbi_trellis():
    """For each next state, find the two predecessors and the expected outputs.
    The two predecessors differ in the bit that was shifted out (LSB of prev)."""
    pred = np.zeros((NUM_STATES, 2), dtype=int)
    sign1 = np.zeros((NUM_STATES, 2))
    sign2 = np.zeros((NUM_STATES, 2))
    for ns in range(NUM_STATES):
        u = ns >> 5
        for b in range(2):
            ps = ((ns & 0x1F) << 1) | b
            pred[ns, b] = ps
            sign1[ns, b] = 1 - 2 * int(_OUT_T1[ps, u])
            sign2[ns, b] = 1 - 2 * int(_OUT_T2[ps, u])
    return pred, sign1, sign2


_PRED, _SIGN1, _SIGN2 = _build_viterbi_trellis()


class ViterbiDecoder:
    def __init__(self, traceback: int = 96):
        self._tb = traceback
        self._metrics = np.full(NUM_STATES, -np.inf)
        self._metrics[0] = 0.0
        self._decisions: list[np.ndarray] = []

    def decode(self, soft: np.ndarray) -> np.ndarray:
        soft = np.asarray(soft, dtype=float)
        out = []
        rows = np.arange(NUM_STATES)
        for i in range(0, soft.size - 1, 2):
            s1, s2 = soft[i], soft[i + 1]
            cand = self._metrics[_PRED] + s1 * _SIGN1 + s2 * _SIGN2
            choice = np.argmax(cand, axis=1)
            self._metrics = cand[rows, choice]
            self._metrics -= self._metrics.max()
            self._decisions.append(choice.astype(np.uint8))
            if len(self._decisions) == 2 * self._tb:
                bits = self._traceback(int(np.argmax(self._metrics)))
                out.append(bits[:self._tb])
                del self._decisions[:self._tb]
        return np.concatenate(out) if out else np.zeros(0, dtype=np.uint8)

    def flush(self, terminated: bool = True) -> np.ndarray:
        end = 0 if terminated else int(np.argmax(self._metrics))
        return self._traceback(end)

    def _traceback(self, state: int) -> np.ndarray:
        bits = np.empty(len(self._decisions), dtype=np.uint8)
        for i in range(len(self._decisions) - 1, -1, -1):
            bits[i] = state >> 5
            state = _PRED[state, self._decisions[i][state]]
        return bits


# ---------------------------------------------------------------------------
# Interleaver
# ---------------------------------------------------------------------------

def _interleave_perm(rows: int, cols: int, row_step: int, col_dec: int) -> np.ndarray:
    n = rows * cols
    k = np.arange(n)
    load_row = (k * row_step) % rows
    load_col = k // rows
    load_idx = load_row * cols + load_col
    diag = np.repeat(np.arange(cols), rows)
    frow = np.tile(np.arange(rows), cols)
    fcol = (diag - col_dec * frow) % cols
    fetch_idx = frow * cols + fcol
    cell_to_input = np.empty(n, dtype=int)
    cell_to_input[load_idx] = np.arange(n)
    return cell_to_input[fetch_idx]


# ---------------------------------------------------------------------------
# Mode specification
# ---------------------------------------------------------------------------

class ModeSpec:
    def __init__(self, rate: int = 2400, interleave: str = "short"):
        if rate not in _FRAME_LAYOUT:
            raise ValueError(f"unsupported rate {rate}")
        if interleave not in ("short", "long", "zero"):
            raise ValueError(f"unsupported interleave {interleave}")
        if rate == 4800:
            interleave = "zero"
        self.rate = rate
        self.interleave = interleave
        bps, tps, unk, kn, cr = _FRAME_LAYOUT[rate]
        self.bits_per_sym = bps
        self.tribits_per_sym = tps
        self.unknown_per_frame = unk
        self.known_per_frame = kn
        self.coding_rate = cr
        self.repetitions = _REPETITIONS[rate]
        self.coded = cr is not None
        sync = "long" if interleave == "long" else "short"
        self.d1, self.d2 = _TABLE_XI[(rate, sync)]
        self.preamble_segments = _PREAMBLE_SEGMENTS[sync]
        self.preamble_count_start = _PREAMBLE_COUNT_START[sync]
        if interleave != "zero" and rate in _INTERLEAVER:
            r, c = _INTERLEAVER[rate][interleave]
            self.il_rows, self.il_cols = r, c
            row_step = 7 if rate == 75 else 9
            col_dec = 7 if rate == 75 else 17
            self.il_perm = _interleave_perm(r, c, row_step, col_dec)
            inv = np.empty_like(self.il_perm)
            inv[self.il_perm] = np.arange(self.il_perm.size)
            self.il_inv = inv
        else:
            self.il_rows = self.il_cols = None
            self.il_perm = self.il_inv = None

    @property
    def frame_symbols(self) -> int | None:
        if self.rate == 75:
            return None
        return self.unknown_per_frame + self.known_per_frame

    @property
    def channel_bits_per_frame(self) -> int | None:
        if self.rate == 75:
            return None
        return self.unknown_per_frame * self.bits_per_sym


# ---------------------------------------------------------------------------
# Root-raised-cosine pulse shaping
# ---------------------------------------------------------------------------

def _rrc(sps: int, alpha: float = 0.35, span: int = 8) -> np.ndarray:
    t = np.arange(-span * sps, span * sps + 1) / sps
    h = np.empty_like(t)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-12:
            h[i] = 1 - alpha + 4 * alpha / np.pi
        elif abs(abs(4 * alpha * ti) - 1) < 1e-9:
            h[i] = (alpha / np.sqrt(2)) * (
                (1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha))
                + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
        else:
            num = np.sin(np.pi * ti * (1 - alpha)) + 4 * alpha * ti * np.cos(np.pi * ti * (1 + alpha))
            h[i] = num / (np.pi * ti * (1 - (4 * alpha * ti) ** 2))
    return h / np.sqrt(np.sum(h ** 2))


# ---------------------------------------------------------------------------
# Modulator
# ---------------------------------------------------------------------------

class MilStdModulator:
    """8-PSK serial-tone modulator for MIL-STD-188-110 / STANAG 4285."""

    def __init__(self, fs: int = 8000, rate: int = 2400, interleave: str = "short",
                 center: float = CARRIER_HZ):
        self.fs = fs
        self.center = center
        self.mode = ModeSpec(rate, interleave)
        self._sps = FS_INTERNAL // SYMBOL_RATE  # 4
        self._rrc = _rrc(self._sps, alpha=0.35, span=8)
        self._phase = 0.0
        self._encoder = ConvEncoder() if self.mode.coded else None
        self._data_sym_pos = 0

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

    def _make_preamble(self) -> np.ndarray:
        segs = []
        for count in range(self.mode.preamble_count_start, -1, -1):
            c1 = 4 | ((count >> 4) & 3)
            c2 = 4 | ((count >> 2) & 3)
            c3 = 4 | (count & 3)
            syms = list(_PREAMBLE_PREFIX) + [self.mode.d1, self.mode.d2, c1, c2, c3] + list(_PREAMBLE_SUFFIX)
            segs.extend(syms)
        channel_syms = np.array(segs, dtype=np.uint8)
        tribits = _TABLE_XIII[channel_syms].reshape(-1)
        sync_seq = np.tile(SYNC_SCRAMBLER, (len(tribits) + 31) // 32)[:len(tribits)]
        return _scramble(tribits, sync_seq)

    def _encode_data(self, user_bits: np.ndarray) -> np.ndarray:
        bits = np.asarray(user_bits, dtype=np.uint8)
        if self._encoder is not None:
            coded = self._encoder.encode(bits)
            if self.mode.repetitions > 1:
                pairs = coded.reshape(-1, 2)
                coded = np.repeat(pairs, self.mode.repetitions, axis=0).reshape(-1)
        else:
            coded = bits
        if self.mode.il_perm is not None:
            block_size = self.mode.il_perm.size
            pad = (-len(coded)) % block_size
            if pad:
                coded = np.concatenate([coded, np.zeros(pad, dtype=np.uint8)])
            blocks = coded.reshape(-1, block_size)
            coded = np.concatenate([b[self.mode.il_perm] for b in blocks])
        return coded

    def _channel_bits_to_tribits(self, channel_bits: np.ndarray) -> np.ndarray:
        bps = self.mode.bits_per_sym
        if self.mode.rate == 75:
            raise NotImplementedError("75 bps not yet supported")
        n_syms = len(channel_bits) // bps
        bits = channel_bits[:n_syms * bps].reshape(-1, bps)
        if bps == 3:
            vals = (bits[:, 0] << 2) | (bits[:, 1] << 1) | bits[:, 2]
            mgd = _MGD3[vals]
        elif bps == 2:
            vals = (bits[:, 0] << 1) | bits[:, 1]
            mgd = _MGD2[vals]
        else:
            mgd = bits.flatten()
        if bps == 1:
            return _MAP_1BIT[mgd]
        elif bps == 2:
            return _MAP_2BIT[mgd]
        else:
            return mgd.astype(np.uint8)

    def _frame_tribits(self, data_tribits: np.ndarray, frame_idx: int, frames_per_block: int) -> np.ndarray:
        unk = self.mode.unknown_per_frame
        kn = self.mode.known_per_frame
        frames = []
        for i in range(0, len(data_tribits), unk):
            chunk = data_tribits[i:i + unk]
            if len(chunk) < unk:
                chunk = np.concatenate([chunk, np.zeros(unk - len(chunk), dtype=np.uint8)])
            probe = np.zeros(kn, dtype=np.uint8)
            pos = frame_idx % frames_per_block
            if pos == frames_per_block - 2:
                probe[:16] = _TABLE_XIII[self.mode.d1][:16]
            elif pos == frames_per_block - 1:
                probe[:16] = _TABLE_XIII[self.mode.d2][:16]
            frames.append(np.concatenate([chunk, probe]))
            frame_idx += 1
        return np.concatenate(frames) if frames else np.zeros(0, dtype=np.uint8), frame_idx

    def transmit(self, data: bytes) -> np.ndarray:
        user_bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8), bitorder="little")
        all_bits = np.concatenate([user_bits, EOM_BITS, np.zeros(144, dtype=np.uint8)])
        channel_bits = self._encode_data(all_bits)
        data_tribits = self._channel_bits_to_tribits(channel_bits)
        fs = self.mode.frame_symbols
        if fs is None:
            raise NotImplementedError("75 bps")
        fpb = (self.mode.il_perm.size if self.mode.il_perm is not None else
               int(SYMBOL_RATE * 0.6)) // fs
        framed, _ = self._frame_tribits(data_tribits, 0, fpb)
        scr_seq = np.tile(DATA_SCRAMBLER, (len(framed) + _SCRAMBLER_PERIOD - 1) // _SCRAMBLER_PERIOD)[:len(framed)]
        scrambled = _scramble(framed, scr_seq)
        preamble = self._make_preamble()
        all_tribits = np.concatenate([preamble, scrambled])
        phasors = self._tribits_to_phasors(all_tribits)
        baseband = self._pulse_shape(phasors)
        return self._to_audio(baseband)

    def idle(self, duration: float) -> np.ndarray:
        return np.zeros(int(self.fs * duration), np.int16)


# ---------------------------------------------------------------------------
# Demodulator
# ---------------------------------------------------------------------------

class MilStdDemodulator:
    """8-PSK serial-tone demodulator for MIL-STD-188-110 / STANAG 4285."""

    def __init__(self, fs: int = 8000, rate: int = 2400, interleave: str = "short",
                 center: float = CARRIER_HZ):
        self.fs = fs
        self.center = center
        self.mode = ModeSpec(rate, interleave)
        self._sps = FS_INTERNAL // SYMBOL_RATE
        self._rrc = _rrc(self._sps, alpha=0.35, span=8)

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

    def _find_preamble(self, filtered: np.ndarray) -> int | None:
        """Find the sample offset where the preamble starts.
        Uses cross-correlation with the first segment and returns the earliest
        strong peak, since the repeating preamble gives many near-equal peaks."""
        preamble_tribits = self._make_preamble_template()
        preamble_phasors = np.exp(1j * np.pi / 4 * preamble_tribits.astype(float))
        sps = self._sps
        tpl = preamble_phasors[:32]
        tpl_norm = np.linalg.norm(tpl)
        best_corr, best_off = 0, 0
        for off in range(sps):
            syms = filtered[off::sps]
            if len(syms) < len(tpl) + 10:
                continue
            corr = np.abs(np.correlate(syms, tpl, mode="valid"))
            for i in range(min(len(corr), 200)):
                seg_norm = np.linalg.norm(syms[i:i + len(tpl)])
                if seg_norm < 1e-12:
                    continue
                c = corr[i] / (seg_norm * tpl_norm)
                if c > 0.9:
                    sample_off = off + i * sps
                    if best_corr == 0 or sample_off < best_off:
                        best_corr = c
                        best_off = sample_off
                    break
        if best_corr < 0.4:
            return None
        return best_off

    def _make_preamble_template(self) -> np.ndarray:
        segs = []
        for count in range(self.mode.preamble_count_start, -1, -1):
            c1 = 4 | ((count >> 4) & 3)
            c2 = 4 | ((count >> 2) & 3)
            c3 = 4 | (count & 3)
            syms = list(_PREAMBLE_PREFIX) + [self.mode.d1, self.mode.d2, c1, c2, c3] + list(_PREAMBLE_SUFFIX)
            segs.extend(syms)
        channel_syms = np.array(segs, dtype=np.uint8)
        tribits = _TABLE_XIII[channel_syms].reshape(-1)
        sync_seq = np.tile(SYNC_SCRAMBLER, (len(tribits) + 31) // 32)[:len(tribits)]
        return _scramble(tribits, sync_seq)

    def _demod_symbols(self, filtered: np.ndarray, start: int, count: int) -> np.ndarray:
        indices = start + np.arange(count) * self._sps
        indices = np.clip(indices, 0, len(filtered) - 1)
        return filtered[indices]

    def _symbols_to_soft(self, syms: np.ndarray) -> np.ndarray:
        phases = np.angle(syms)
        tribits = np.round(phases / (np.pi / 4)) % 8
        return tribits.astype(np.uint8)

    def _hard_to_soft_fec(self, tribits: np.ndarray) -> np.ndarray:
        bps = self.mode.bits_per_sym
        if bps == 3:
            inv = _MGD3_INV[tribits]
            bits = np.empty(len(inv) * 3, dtype=np.uint8)
            bits[0::3] = (inv >> 2) & 1
            bits[1::3] = (inv >> 1) & 1
            bits[2::3] = inv & 1
        elif bps == 2:
            mapped = np.zeros(len(tribits), dtype=np.uint8)
            for v in range(4):
                mapped[tribits == _MAP_2BIT[v]] = v
            inv = _MGD2_INV[mapped]
            bits = np.empty(len(inv) * 2, dtype=np.uint8)
            bits[0::2] = (inv >> 1) & 1
            bits[1::2] = inv & 1
        else:
            bits = (tribits >= 4).astype(np.uint8)
        soft = 1.0 - 2.0 * bits.astype(float)
        return soft

    def feed(self, audio: np.ndarray) -> bytes:
        x = self._resample_to_internal(np.asarray(audio, dtype=float))
        bb = self._to_baseband(x)
        filt = self._matched_filter(bb)
        preamble_tpl = self._make_preamble_template()
        preamble_len = len(preamble_tpl)
        start = self._find_preamble(filt)
        if start is None:
            return b""
        data_start = start + preamble_len * self._sps
        avail = (len(filt) - data_start) // self._sps
        if avail <= 0:
            return b""
        syms = self._demod_symbols(filt, data_start, avail)
        scr_seq = np.tile(DATA_SCRAMBLER, (avail + _SCRAMBLER_PERIOD - 1) // _SCRAMBLER_PERIOD)[:avail]
        descrambled = _descramble(self._symbols_to_soft(syms), scr_seq)
        fs = self.mode.frame_symbols
        if fs is None:
            return b""
        n_frames = len(descrambled) // fs
        data_tribits = []
        for i in range(n_frames):
            frame = descrambled[i * fs:(i + 1) * fs]
            data_tribits.append(frame[:self.mode.unknown_per_frame])
        if not data_tribits:
            return b""
        all_data = np.concatenate(data_tribits)
        soft = self._hard_to_soft_fec(all_data)
        if self.mode.il_inv is not None:
            block_size = self.mode.il_inv.size
            usable = (len(soft) // block_size) * block_size
            if usable == 0:
                return b""
            blocks = soft[:usable].reshape(-1, block_size)
            soft = np.concatenate([b[self.mode.il_inv] for b in blocks])
        if self.mode.repetitions > 1:
            r = self.mode.repetitions
            usable = (len(soft) // (2 * r)) * (2 * r)
            soft = soft[:usable].reshape(-1, r, 2).sum(axis=1).reshape(-1)
        if self.mode.coded:
            vit = ViterbiDecoder()
            bits = np.concatenate([vit.decode(soft), vit.flush()])
        else:
            bits = (soft < 0).astype(np.uint8)
        eom_soft = 1.0 - 2.0 * EOM_BITS.astype(float)
        corr = np.correlate(1.0 - 2.0 * bits.astype(float), eom_soft, mode="valid")
        eom_pos = None
        if len(corr) > 0:
            best = int(np.argmax(corr))
            if corr[best] > 20:
                eom_pos = best
        if eom_pos is not None:
            bits = bits[:eom_pos]
        pad = (-len(bits)) % 8
        if pad:
            bits = np.concatenate([bits, np.zeros(pad, dtype=np.uint8)])
        out = np.packbits(bits.reshape(-1, 8), bitorder="little", axis=1).flatten()
        return bytes(out)
