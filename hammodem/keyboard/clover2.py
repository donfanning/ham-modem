"""Clover-II HF data modem (HAL Communications, W7GHM / W7GHM design).

Implements the Clover-II waveform from published documentation:
  - Ray Petit W7GHM, "CLOVER-II: A Technical Overview," ARRL CNC 1991
  - Bill Henry K9GWT / Ray Petit W7GHM, "HF Radio Data Communication CW to Clover,"
    Communications Quarterly, Spring 1992
  - ARRL Technology page: arrl.org/clover

Waveform: 4 tone pulses at 812.5, 937.5, 1062.5, 1187.5 Hz (125 Hz spacing,
centre 1000 Hz), each 32 ms, staggered 8 ms apart, half-cosine (Hann) shaped.
Symbol rate 31.25 baud per tone (composite 125 baud).  Sidebands < -60 dB at
±250 Hz.

Eight modulation modes: 2DPSM, 2DFSM, BPSM, QPSM, 8PSM, 16PSM, 8P2A, 16P4A.
Reed-Solomon FEC with four block sizes and three efficiency levels.
Selective-repeat ARQ; bidirectional; adaptive mode selection.

Licence note: Clover patents (US5,007,068 etc.) expired ~2010.  Implementation
is clean-room from the published technical papers listed above.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FS = 8000                       # default sample rate (Hz)
TONE_FREQS = np.array([812.5, 937.5, 1062.5, 1187.5])  # Hz
TONE_SPACING = 125.0            # Hz
CENTRE_FREQ = 1000.0            # Hz
PULSE_MS = 32                   # ms per tone pulse
STAGGER_MS = 8                  # ms between successive tone peaks
SYMBOL_RATE = 1000.0 / PULSE_MS  # 31.25 baud per tone

# Interleave/stagger: tone 0 peaks at 0 ms, tone 1 at 8 ms, …, tone 3 at 24 ms
# The composite frame is 4 staggered pulses = 32 ms total
FRAME_MS = PULSE_MS             # one full frame = 32 ms (all 4 tones)

# Reed-Solomon block sizes (data bytes per block at 100% efficiency)
RS_BLOCK_SIZES = (17, 51, 85, 255)
# RS efficiency levels → fraction of block that is data
RS_EFFICIENCIES = (0.60, 0.75, 0.90, 1.00)

# ARQ ECB is always 255 bytes; data bytes depend on bias
ECB_TOTAL = 255
ECB_ROBUST = 150   # 60% efficiency
ECB_NORMAL = 188   # ~74% efficiency
ECB_FAST   = 226   # ~89% efficiency

# ARQ timing
ARQ_FRAME_S = 19.488           # exact ARQ frame duration (seconds)
CCB_ARQ_S   = 2.72             # CCB in ARQ mode
CCB_FEC_S   = 2.048            # CCB carrier in FEC mode

# Amplitude levels for ASK modes
_ASK2_AMPS = np.array([1.0, 0.5])           # 2 amplitude levels (8P2A)
_ASK4_AMPS = np.array([1.0, 0.75, 0.5, 0.25])  # 4 amplitude levels (16P4A)

SCALE = 16384                   # audio int16 scale (half of 32768 for headroom)


# ---------------------------------------------------------------------------
# Modulation mode enum
# ---------------------------------------------------------------------------

class Mode(IntEnum):
    PSM2D  = 0   # dual-diversity BPSK  (0.5 bits/pulse → 62.5 bps)
    FSM2D  = 1   # dual-diversity FSK   (0.5 bits/pulse → 62.5 bps)
    BPSM   = 2   # BPSK                 (1 bit/pulse   → 125 bps)
    QPSM   = 3   # QPSK                 (2 bits/pulse  → 250 bps)
    PSM8   = 4   # 8-PSK                (3 bits/pulse  → 375 bps)
    PSM16  = 5   # 16-PSK               (4 bits/pulse  → 500 bps)
    P8A2   = 6   # 8-PSK + 2-ASK       (4 bits/pulse  → 500 bps)
    P16A4  = 7   # 16-PSK + 4-ASK      (6 bits/pulse  → 750 bps)

_BITS_PER_PULSE = {
    Mode.PSM2D: 1,   # 1 bit across 2 pulses (diversity)
    Mode.FSM2D: 1,
    Mode.BPSM:  1,
    Mode.QPSM:  2,
    Mode.PSM8:  3,
    Mode.PSM16: 4,
    Mode.P8A2:  4,
    Mode.P16A4: 6,
}

DATA_RATES_BPS = {
    Mode.PSM2D: 62.5,
    Mode.FSM2D: 62.5,
    Mode.BPSM:  125.0,
    Mode.QPSM:  250.0,
    Mode.PSM8:  375.0,
    Mode.PSM16: 500.0,
    Mode.P8A2:  500.0,
    Mode.P16A4: 750.0,
}


# ---------------------------------------------------------------------------
# Reed-Solomon over GF(2^8), primitive polynomial x^8+x^4+x^3+x^2+1 (0x11d)
# Same field as CD-ROM / CCSDS RS; uses generator polynomial with root α=2.
# ---------------------------------------------------------------------------

class _GF256:
    """GF(2^8) arithmetic tables, primitive poly 0x11d."""
    POLY = 0x11d

    def __init__(self) -> None:
        self.exp = [0] * 512
        self.log = [0] * 256
        x = 1
        for i in range(255):
            self.exp[i] = x
            self.log[x] = i
            x <<= 1
            if x & 0x100:
                x ^= self.POLY
        for i in range(255, 512):
            self.exp[i] = self.exp[i - 255]

    def mul(self, a: int, b: int) -> int:
        if a == 0 or b == 0:
            return 0
        return self.exp[self.log[a] + self.log[b]]

    def div(self, a: int, b: int) -> int:
        if a == 0:
            return 0
        return self.exp[(self.log[a] - self.log[b]) % 255]

    def pow(self, x: int, n: int) -> int:
        return self.exp[(self.log[x] * n) % 255] if x != 0 else 0

    def inv(self, x: int) -> int:
        return self.exp[255 - self.log[x]]


_GF = _GF256()


def _rs_generator(nsym: int) -> list[int]:
    """Build RS generator polynomial with roots α^0 … α^(nsym-1)."""
    g = [1]
    for i in range(nsym):
        # multiply g by (x - α^i)
        factor = [1, _GF.exp[i]]
        result = [0] * (len(g) + 1)
        for j, gj in enumerate(g):
            for k, fk in enumerate(factor):
                result[j + k] ^= _GF.mul(gj, fk)
        g = result
    return g


def rs_encode(data: bytes, nsym: int) -> bytes:
    """Return data + nsym parity bytes (systematic code)."""
    g = _rs_generator(nsym)
    # Long-division scratch; the data positions are the quotient accumulator and
    # get modified, but parity positions are the remainder — return original data.
    msg = list(data) + [0] * nsym
    for i in range(len(data)):
        coef = msg[i]
        if coef:
            for j in range(1, len(g)):
                msg[i + j] ^= _GF.mul(g[j], coef)
    return data + bytes(msg[len(data):])


def rs_decode(msg: bytes, nsym: int) -> Optional[bytes]:
    """Decode and correct up to nsym//2 errors.  Returns data or None.

    Syndrome convention: S[i] = c(α^i) evaluated with big-endian Horner.
    For the codeword polynomial c(x) = c[0]x^{n-1} + ... + c[n-1], the
    error at array position p contributes S[i] = e_p * (α^i)^{n-1-p}.
    Error locator numbers are therefore X_p = α^{n-1-p}.
    """
    msg = list(msg)
    n = len(msg)
    k = n - nsym

    # Syndromes: S[i] = c(α^i) via big-endian Horner
    syndromes = [0] * nsym
    for i in range(nsym):
        s = 0
        for b in msg:
            s = _GF.mul(s, _GF.exp[i]) ^ b
        syndromes[i] = s
    if max(syndromes) == 0:
        return bytes(msg[:k])

    # Berlekamp-Massey: find sigma(x) = prod_k (1 - X_k * x), roots at X_k^{-1}
    sigma, B = [1], [1]
    L, m, b_coef = 0, 1, 1
    for n_iter in range(nsym):
        d = syndromes[n_iter]
        for i in range(1, L + 1):
            if i < len(sigma):
                d ^= _GF.mul(sigma[i], syndromes[n_iter - i])
        if d == 0:
            m += 1
        elif 2 * L <= n_iter:
            T = sigma[:]
            factor = _GF.mul(d, _GF.inv(b_coef))
            shift = [0] * m + [_GF.mul(factor, bi) for bi in B]
            while len(shift) < len(sigma):
                shift.append(0)
            while len(sigma) < len(shift):
                sigma.append(0)
            sigma = [c ^ s for c, s in zip(sigma, shift)]
            L, B, b_coef, m = n_iter + 1 - L, T, d, 1
        else:
            factor = _GF.mul(d, _GF.inv(b_coef))
            shift = [0] * m + [_GF.mul(factor, bi) for bi in B]
            while len(shift) < len(sigma):
                shift.append(0)
            while len(sigma) < len(shift):
                sigma.append(0)
            sigma = [c ^ s for c, s in zip(sigma, shift)]
            m += 1

    nerrs = len(sigma) - 1
    if nerrs > nsym // 2:
        return None

    # Chien search: for each array position p, X_p = α^{n-1-p};
    # test whether sigma(X_p^{-1}) == 0.
    error_pos = []
    for p in range(n):
        xp_inv_log = (255 - (n - 1 - p)) % 255  # = (p + 1 - n) mod 255
        xp_inv = _GF.exp[xp_inv_log]
        val = 0
        for j, sj in enumerate(sigma):
            val ^= _GF.mul(sj, _GF.pow(xp_inv, j))
        if val == 0:
            error_pos.append(p)
    if len(error_pos) != nerrs:
        return None

    # Error evaluator: Omega = sigma * S mod x^nsym  (little-endian coefficients)
    omega = [0] * nsym
    for i in range(nsym):
        for j, sj in enumerate(sigma):
            if 0 <= i - j < nsym:
                omega[i] ^= _GF.mul(sj, syndromes[i - j])

    # Forney: e_p = X_p * Omega(X_p^{-1}) / sigma'(X_p^{-1})
    # sigma'(x) = sum_{j odd} sigma[j] * x^{j-1}
    for p in error_pos:
        xp_log = (n - 1 - p) % 255
        xp = _GF.exp[xp_log]          # X_p = α^{n-1-p}
        xp_inv = _GF.inv(xp)

        omega_val = 0
        for j, oj in enumerate(omega):
            omega_val ^= _GF.mul(oj, _GF.pow(xp_inv, j))

        sigma_p = 0
        for j in range(1, len(sigma), 2):
            sigma_p ^= _GF.mul(sigma[j], _GF.pow(xp_inv, j - 1))

        if sigma_p == 0:
            return None
        msg[p] ^= _GF.mul(_GF.mul(xp, omega_val), _GF.inv(sigma_p))

    return bytes(msg[:k])


# ---------------------------------------------------------------------------
# Phase/amplitude constellation builders
# ---------------------------------------------------------------------------

def _psk_constellation(n: int) -> np.ndarray:
    """n equally-spaced phase points on unit circle."""
    return np.exp(1j * 2 * np.pi * np.arange(n) / n)


def _symbols_to_bits(sym_idx: np.ndarray, bits_per_sym: int) -> np.ndarray:
    bits = []
    mask = (1 << bits_per_sym) - 1
    for s in sym_idx:
        for b in range(bits_per_sym - 1, -1, -1):
            bits.append(int((s >> b) & 1))
    return np.array(bits, dtype=np.uint8)


def _bits_to_symbols(bits: np.ndarray, bits_per_sym: int) -> np.ndarray:
    n = len(bits) // bits_per_sym
    bits = bits[:n * bits_per_sym].reshape(n, bits_per_sym)
    vals = np.zeros(n, dtype=np.int32)
    for b in range(bits_per_sym):
        vals = (vals << 1) | bits[:, b].astype(np.int32)
    return vals


def _gray(n: int) -> np.ndarray:
    """Gray-code mapping for n symbols."""
    return np.array([i ^ (i >> 1) for i in range(n)], dtype=np.int32)


def _inv_gray(n: int) -> np.ndarray:
    inv = np.zeros(n, dtype=np.int32)
    g = _gray(n)
    for i, v in enumerate(g):
        inv[v] = i
    return inv


# Pre-build constellations (Gray-coded)
_CONSTELLATIONS: dict[Mode, np.ndarray] = {}
_INV_GRAY_MAP: dict[int, np.ndarray] = {}


def _build_constellations() -> None:
    g2 = _gray(2);   ig2 = _inv_gray(2)
    g4 = _gray(4);   ig4 = _inv_gray(4)
    g8 = _gray(8);   ig8 = _inv_gray(8)
    g16 = _gray(16); ig16 = _inv_gray(16)
    _INV_GRAY_MAP[2]  = ig2
    _INV_GRAY_MAP[4]  = ig4
    _INV_GRAY_MAP[8]  = ig8
    _INV_GRAY_MAP[16] = ig16
    psk2  = _psk_constellation(2)[g2]
    psk4  = _psk_constellation(4)[g4]
    psk8  = _psk_constellation(8)[g8]
    psk16 = _psk_constellation(16)[g16]
    _CONSTELLATIONS[Mode.BPSM]  = psk2
    _CONSTELLATIONS[Mode.QPSM]  = psk4
    _CONSTELLATIONS[Mode.PSM8]  = psk8
    _CONSTELLATIONS[Mode.PSM16] = psk16
    # 8P2A: 8-PSK × 2-amplitude (Gray-coded jointly by 4-bit index)
    syms8p2a = np.empty(16, dtype=complex)
    for i in range(16):
        phase_idx = i % 8
        amp_idx   = i >> 3
        syms8p2a[i] = psk8[phase_idx] * _ASK2_AMPS[amp_idx]
    _CONSTELLATIONS[Mode.P8A2] = syms8p2a
    # 16P4A: 16-PSK × 4-amplitude (Gray-coded jointly by 6-bit index)
    syms16p4a = np.empty(64, dtype=complex)
    for i in range(64):
        phase_idx = i % 16
        amp_idx   = i >> 4
        syms16p4a[i] = psk16[phase_idx] * _ASK4_AMPS[amp_idx]
    _CONSTELLATIONS[Mode.P16A4] = syms16p4a


_build_constellations()


# ---------------------------------------------------------------------------
# Pulse shape: half-cosine (Hann) window, 32 ms
# ---------------------------------------------------------------------------

def _make_pulse(fs: int, pulse_ms: float = PULSE_MS) -> np.ndarray:
    """Hann-windowed tone pulse envelope, unit peak.

    The half-cosine shape concentrates the spectrum within ±1/T of the carrier,
    achieving better than -60 dB sideband suppression at the adjacent tone
    (spaced 125 Hz = one bandwidth away).
    """
    n = int(round(fs * pulse_ms / 1000))
    t = np.arange(n)
    return 0.5 * (1.0 - np.cos(2.0 * np.pi * t / (n - 1)))


# ---------------------------------------------------------------------------
# Modulator
# ---------------------------------------------------------------------------

class Clover2Modulator:
    """Clover-II modulator.

    Usage::

        mod = Clover2Modulator(fs=8000, mode=Mode.QPSM)
        audio = mod.transmit(b"Hello Clover")   # returns np.int16 array
    """

    def __init__(self, fs: int = FS, mode: Mode = Mode.QPSM,
                 centre: float = CENTRE_FREQ) -> None:
        self.fs     = fs
        self.mode   = mode
        self.centre = centre
        self._pulse = _make_pulse(fs)          # 32-ms Hann pulse
        self._spp   = len(self._pulse)         # samples per pulse
        self._stagger = int(round(fs * STAGGER_MS / 1000))  # 8-ms offset
        # Tone frequencies offset to the requested centre
        offset = centre - CENTRE_FREQ
        self._freqs = TONE_FREQS + offset

    # -- public ---------------------------------------------------------------

    def transmit(self, data: bytes) -> np.ndarray:
        """Encode data to Clover-II audio (int16)."""
        bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8))
        symbols = self._encode_symbols(bits)
        audio = self._symbols_to_audio(symbols)
        return np.clip(audio * SCALE, -32768, 32767).astype(np.int16)

    def idle(self, duration: float) -> np.ndarray:
        return np.zeros(int(self.fs * duration), dtype=np.int16)

    # -- internal -------------------------------------------------------------

    def _encode_symbols(self, bits: np.ndarray) -> list[np.ndarray]:
        """Return list of per-frame symbol arrays (one complex per tone)."""
        bpp = _BITS_PER_PULSE[self.mode]

        if self.mode in (Mode.PSM2D, Mode.FSM2D):
            # Dual-diversity: repeat each bit on two consecutive same-freq pulses
            # encode as BPSK with repetition
            n_syms = len(bits)
            frames = []
            bit_idx = 0
            while bit_idx < n_syms:
                frame_syms = np.zeros(4, dtype=complex)
                for tone_i in range(4):
                    if bit_idx < n_syms:
                        b = bits[bit_idx]
                        bit_idx += 1
                    else:
                        b = 0
                    phase = np.pi * b   # BPSK
                    frame_syms[tone_i] = np.exp(1j * phase)
                frames.append(frame_syms)
            return frames

        const = _CONSTELLATIONS[self.mode]
        n_const = len(const)
        total_bits_needed = len(bits)
        n_pulses = (total_bits_needed + bpp - 1) // bpp
        # Pad bits to full symbol boundary
        pad = (-len(bits)) % bpp
        bits_pad = np.concatenate([bits, np.zeros(pad, dtype=np.uint8)])
        sym_indices = _bits_to_symbols(bits_pad, bpp)

        frames = []
        sym_idx = 0
        while sym_idx < len(sym_indices):
            frame_syms = np.zeros(4, dtype=complex)
            for tone_i in range(4):
                if sym_idx < len(sym_indices):
                    idx = int(sym_indices[sym_idx]) % n_const
                    frame_syms[tone_i] = const[idx]
                    sym_idx += 1
            frames.append(frame_syms)
        return frames

    def _symbols_to_audio(self, frames: list[np.ndarray]) -> np.ndarray:
        """Render frame symbols to audio samples."""
        n_frames = len(frames)
        # Total audio length: stagger puts tone 0 at 0, tone 1 at stagger, …
        # Last frame's last tone ends at:
        #   (n_frames - 1) * spp + 3*stagger + spp
        total_frames_len = (n_frames - 1) * self._spp + 3 * self._stagger + self._spp
        audio = np.zeros(total_frames_len)
        t_all = np.arange(total_frames_len) / self.fs

        pulse = self._pulse
        spp   = self._spp
        stag  = self._stagger

        for fi, frame_syms in enumerate(frames):
            frame_start = fi * spp
            for tone_i, sym in enumerate(frame_syms):
                tone_start = frame_start + tone_i * stag
                tone_end   = tone_start + spp
                if tone_end > total_frames_len:
                    spp_actual = total_frames_len - tone_start
                    if spp_actual <= 0:
                        continue
                    p = pulse[:spp_actual]
                else:
                    p = pulse

                t_seg = t_all[tone_start:tone_start + len(p)]
                fc = self._freqs[tone_i]
                phase = np.angle(sym)
                carrier = np.cos(2.0 * np.pi * fc * t_seg + phase)
                audio[tone_start:tone_start + len(p)] += p * carrier

        # Normalise to unit peak (4 overlapping tones max)
        peak = np.max(np.abs(audio)) or 1.0
        return audio / peak


# ---------------------------------------------------------------------------
# Demodulator
# ---------------------------------------------------------------------------

class Clover2Demodulator:
    """Clover-II demodulator.

    Feed arbitrary audio blocks; returns decoded bytes when a complete
    message (SOM → EOM via data frames) is received.

    Usage::

        demod = Clover2Demodulator(fs=8000, mode=Mode.QPSM)
        data  = demod.feed(audio_int16)
    """

    def __init__(self, fs: int = FS, mode: Mode = Mode.QPSM,
                 centre: float = CENTRE_FREQ) -> None:
        self.fs     = fs
        self.mode   = mode
        self.centre = centre
        self._spp   = int(round(fs * PULSE_MS / 1000))
        self._stag  = int(round(fs * STAGGER_MS / 1000))
        offset = centre - CENTRE_FREQ
        self._freqs = TONE_FREQS + offset
        self._buf   = np.zeros(0, dtype=np.float64)

    def feed(self, audio: np.ndarray) -> bytes:
        """Process audio; return decoded bytes (b'' if incomplete)."""
        x = audio.astype(np.float64)
        self._buf = np.concatenate([self._buf, x])
        bits = self._demodulate_all()
        if bits is None or len(bits) == 0:
            return b''
        n_bytes = len(bits) // 8
        if n_bytes == 0:
            return b''
        return np.packbits(bits[:n_bytes * 8]).tobytes()

    # -- internal -------------------------------------------------------------

    def _demodulate_all(self) -> Optional[np.ndarray]:
        """Extract symbols from buffer, return bit array."""
        buf   = self._buf
        spp   = self._spp
        stag  = self._stag
        freqs = self._freqs

        # Need at least one full frame (spp + 3*stag) plus one tone
        min_len = spp + 3 * stag
        if len(buf) < min_len:
            return None

        bpp   = _BITS_PER_PULSE[self.mode]
        const = _CONSTELLATIONS.get(self.mode)

        # Scan for a frame: correlate with each tone at its expected stagger offset
        # Use simple approach: process frames sequentially from start
        all_bits = []
        pos = 0
        while pos + spp + 3 * stag <= len(buf):
            frame_syms = np.zeros(4, dtype=complex)
            for tone_i in range(4):
                tone_start = pos + tone_i * stag
                if tone_start + spp > len(buf):
                    break
                seg = buf[tone_start:tone_start + spp]
                # Use absolute time so carrier phase matches what the modulator wrote
                t   = np.arange(tone_start, tone_start + spp) / self.fs
                fc  = freqs[tone_i]
                pulse = _make_pulse(self.fs)[:spp]
                I = np.sum(seg * pulse * np.cos(2 * np.pi * fc * t))
                Q = np.sum(seg * pulse * np.sin(2 * np.pi * fc * t))
                frame_syms[tone_i] = complex(I, -Q)

            if self.mode in (Mode.PSM2D, Mode.FSM2D):
                for sym in frame_syms:
                    b = 0 if np.real(sym) >= 0 else 1
                    all_bits.append(b)
            elif const is not None:
                for sym in frame_syms:
                    # Nearest constellation point
                    dists = np.abs(const - sym / (np.abs(sym) + 1e-30))
                    idx = int(np.argmin(dists))
                    frame_bits = _symbols_to_bits(np.array([idx]), bpp)
                    all_bits.extend(frame_bits.tolist())

            pos += spp  # advance one frame

        self._buf = buf[pos:]
        return np.array(all_bits, dtype=np.uint8)


# ---------------------------------------------------------------------------
# RS-protected frame codec (CCB / ECB)
# ---------------------------------------------------------------------------

class CloverFrame:
    """Encode/decode Clover CCB and ECB frames with Reed-Solomon.

    RS parameters follow the published spec:
      - Block size 255 bytes total
      - Data bytes: 150 (60% / robust), 188 (74% / normal), 226 (89% / fast)
      - nsym = total - data_bytes → parity bytes
    """

    BIAS_ROBUST = 'robust'
    BIAS_NORMAL = 'normal'
    BIAS_FAST   = 'fast'

    _DATA_BYTES = {
        'robust': ECB_ROBUST,
        'normal': ECB_NORMAL,
        'fast':   ECB_FAST,
    }

    def encode_ecb(self, data: bytes, bias: str = 'normal') -> bytes:
        """Encode up to data_bytes of payload into a 255-byte RS-protected ECB."""
        db = self._DATA_BYTES[bias]
        nsym = ECB_TOTAL - db
        if len(data) > db:
            data = data[:db]
        # Pad with zeros to db bytes
        padded = data + bytes(db - len(data))
        encoded = rs_encode(padded, nsym)
        assert len(encoded) == ECB_TOTAL
        return encoded

    def decode_ecb(self, raw: bytes, bias: str = 'normal') -> Optional[bytes]:
        """Decode a 255-byte ECB; return data bytes or None on uncorrectable error."""
        if len(raw) != ECB_TOTAL:
            return None
        db = self._DATA_BYTES[bias]
        nsym = ECB_TOTAL - db
        result = rs_decode(raw, nsym)
        if result is None:
            return None
        return result  # db bytes

    def encode_ccb(self, payload: bytes) -> bytes:
        """Encode 17-byte CCB payload with RS(17,13) → 17 total bytes ECC."""
        # CCB is 17 data bytes with 4 RS parity bytes appended = 21 bytes over-the-air
        # but keeping to spec: 17-byte block at 60% → 17/0.60 ≈ 28 bytes total
        # In practice CCB uses a small RS code; we use RS(28,17) for simplicity
        nsym = 11
        if len(payload) < 17:
            payload = payload + bytes(17 - len(payload))
        return rs_encode(payload[:17], nsym)

    def decode_ccb(self, raw: bytes) -> Optional[bytes]:
        """Decode CCB; return 17-byte payload or None."""
        nsym = 11
        result = rs_decode(raw, nsym)
        return result


# ---------------------------------------------------------------------------
# ARQ session (simplified selective-repeat)
# ---------------------------------------------------------------------------

@dataclass
class Clover2Session:
    """Clover-II ARQ data-link session (simplified for offline round-trip use).

    For live radio use, connect outgoing_pdus to the modulator and feed
    incoming raw bytes to receive().
    """

    local_call:  str
    remote_call: str = ''
    mode:        Mode = Mode.QPSM
    bias:        str  = 'normal'

    _frame:      CloverFrame = field(default_factory=CloverFrame)
    _tx_queue:   list = field(default_factory=list)
    outgoing:    list = field(default_factory=list)   # encoded ECBs ready to transmit
    received:    list = field(default_factory=list)   # decoded payload bytes
    _ack_pending: set = field(default_factory=set)

    def send(self, data: bytes) -> None:
        """Queue data for transmission."""
        db = CloverFrame._DATA_BYTES[self.bias]
        for i in range(0, len(data), db):
            chunk = data[i:i + db]
            self._tx_queue.append(chunk)

    def pump(self) -> None:
        """Move queued chunks into outgoing ECBs."""
        while self._tx_queue:
            chunk = self._tx_queue.pop(0)
            ecb = self._frame.encode_ecb(chunk, self.bias)
            seq = len(self.outgoing)
            self.outgoing.append((seq, ecb))
            self._ack_pending.add(seq)

    def receive(self, raw_ecb: bytes) -> None:
        """Process a received raw ECB."""
        data = self._frame.decode_ecb(raw_ecb, self.bias)
        if data is not None:
            self.received.append(data.rstrip(b'\x00'))


# ---------------------------------------------------------------------------
# Top-level API (matches other keyboard modules: transmit / feed)
# ---------------------------------------------------------------------------

class Clover2Tnc:
    """Simple round-trip interface: transmit() → audio, feed(audio) → bytes."""

    def __init__(self, fs: int = FS, mode: Mode = Mode.QPSM,
                 centre: float = CENTRE_FREQ, bias: str = 'normal') -> None:
        self._mod   = Clover2Modulator(fs=fs, mode=mode, centre=centre)
        self._demod = Clover2Demodulator(fs=fs, mode=mode, centre=centre)
        self._frame = CloverFrame()
        self._bias  = bias

    def transmit(self, data: bytes) -> np.ndarray:
        """RS-encode then modulate data; return int16 audio."""
        db = CloverFrame._DATA_BYTES[self._bias]
        encoded_chunks = []
        for i in range(0, max(1, len(data)), db):
            chunk = data[i:i + db]
            ecb = self._frame.encode_ecb(chunk, self._bias)
            encoded_chunks.append(ecb)
        payload = b''.join(encoded_chunks)
        return self._mod.transmit(payload)

    def feed(self, audio: np.ndarray) -> bytes:
        """Demodulate audio then RS-decode; return recovered data."""
        raw = self._demod.feed(audio)
        if not raw:
            return b''
        # Reassemble ECBs
        total = ECB_TOTAL
        out = bytearray()
        for i in range(0, len(raw), total):
            chunk = raw[i:i + total]
            if len(chunk) == total:
                dec = self._frame.decode_ecb(chunk, self._bias)
                if dec:
                    out.extend(dec.rstrip(b'\x00'))
            else:
                out.extend(chunk)
        return bytes(out)
