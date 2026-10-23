"""PACTOR-II-compatible HF ARQ modem (clean-room, black-box implementation).

Physical layer derived from published sources:
  - SCS Packet-Radio Catalogue snippets, WAVECOM Waveform Reference Guide
  - On-air recordings and measurements (docs/PHASE15_REVERSE_ENGINEERING.md)
  - Published DCC papers and ARRL handbook descriptions

Waveform: two DPSK carriers at 900 Hz and 1100 Hz, 100 baud per carrier.
Four speed levels: DBPSK (level 1) → DQPSK (2) → 8-DPSK (3) → 16-DPSK (4).
K=7 rate-1/2 convolutional FEC (same polynomials as MIL-STD, from milstd.py).
Memory ARQ: soft bit values are summed across retransmissions before Viterbi.
ARQ cycle: 1.25 s stop-and-wait (ISS burst ~0.96 s, IRS CS reply ~0.275 s).

Framing note: the exact PACTOR-II (SCS proprietary) packet overhead is not
published.  The byte counts below (DATA_BYTES per level) are derived from the
channel capacity of a 100 Bd/carrier burst with r=1/2 FEC and 3-byte overhead
(1 status + 2 CRC-16).  They are internally consistent and pass round-trip
tests but will not interoperate with real SCS hardware.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .milstd import ConvEncoder, ViterbiDecoder
from ..pactor.code import crc_ccitt, crc_check

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FS = 8000                   # sample rate (Hz) — must be 8 kHz for VoIP
BAUD = 100                  # symbols per second per carrier
SPS = FS // BAUD            # samples per symbol = 80

CARRIER_0 = 900.0           # Hz
CARRIER_1 = 1100.0          # Hz
CARRIERS = np.array([CARRIER_0, CARRIER_1])
CENTER = 1000.0             # Hz (midpoint)
SHIFT = 100.0               # Hz (half-spacing)

BURST_S = 0.960             # ISS data burst (s)
CS_S = 0.275                # IRS control signal (s)
CYCLE_S = 1.250             # ARQ cycle (s)

BURST_SYMS = int(BAUD * BURST_S)     # 96 symbols per carrier per burst
CS_SYMS = int(BAUD * CS_S)           # 27 symbols

_SRRC_SPAN    = 4                     # symbols on each side of SRRC
# Burst includes SRRC filter tail so the last symbol decodes cleanly
BURST_SAMPLES = BURST_SYMS * SPS + _SRRC_SPAN * SPS   # 8000 samples
CS_SAMPLES    = CS_SYMS   * SPS      # 2160 = 0.270 s (≈ 0.275 s)
CYCLE_SAMPLES = int(CYCLE_S * FS)    # 10000

# Preamble: 5 known symbols per carrier used for phase recovery.
PREAMBLE_SYMS = 5
DATA_SYMS = BURST_SYMS - PREAMBLE_SYMS   # 91 symbols of data per carrier

# Bits per symbol per carrier per level
BITS_PER_SYM = {1: 1, 2: 2, 3: 3, 4: 4}
# M = number of DPSK phase states
M_STATES = {1: 2, 2: 4, 3: 8, 4: 16}

# Channel bits available per burst (data region only; r=1/2 FEC → half info bits)
#   level k:  2 carriers × DATA_SYMS × BITS_PER_SYM[k]
_CHANNEL_BITS = {k: 2 * DATA_SYMS * BITS_PER_SYM[k] for k in range(1, 5)}
# Information bits after r=1/2 FEC
_INFO_BITS    = {k: _CHANNEL_BITS[k] // 2 for k in range(1, 5)}
# Total packet bytes (info bits / 8)
_PKT_BYTES    = {k: _INFO_BITS[k] // 8 for k in range(1, 5)}
# Overhead: 1 status byte + 2 CRC bytes
_OVERHEAD = 3
# Net data bytes per packet per level
DATA_BYTES = {k: max(0, _PKT_BYTES[k] - _OVERHEAD) for k in range(1, 5)}
# DATA_BYTES = {1: 9, 2: 20, 3: 32, 4: 45} approximately

# CS reply bits: 12-bit sequence (same as PACTOR-I: CS1/CS2/CS3/CS4/CS4_HSP)
CS_REPLY_BITS = 12

# Gray code tables for DPSK
def _gray(n: int) -> np.ndarray:
    return np.array([i ^ (i >> 1) for i in range(n)], dtype=np.int32)

def _inv_gray(n: int) -> np.ndarray:
    inv = np.zeros(n, dtype=np.int32)
    for i, v in enumerate(_gray(n)):
        inv[v] = i
    return inv

_GRAY_ENC = {m: _gray(m) for m in (2, 4, 8, 16)}
_GRAY_DEC = {m: _inv_gray(m) for m in (2, 4, 8, 16)}

# Preamble phase pattern (alternating 0 and π/2 phase increments)
_PREAMBLE_PHASES = np.array([0, np.pi/2, 0, np.pi/2, 0])


# ---------------------------------------------------------------------------
# Pulse shape (RRC-like: simple raised-cosine with α=0.5, or just boxcar)
# ---------------------------------------------------------------------------

def _make_srrc(sps: int, alpha: float = 0.5, span: int = 4) -> np.ndarray:
    """True square-root raised-cosine FIR.  TX*RX = RC (Nyquist, zero ISI)."""
    n = span * sps + 1
    t = (np.arange(n) - n // 2) / sps  # normalised time (units of symbol period)
    h = np.zeros(n, dtype=np.float64)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-10:
            h[i] = 1.0 - alpha + 4.0 * alpha / np.pi
        elif abs(abs(ti) - 1.0 / (4.0 * alpha)) < 1e-10:
            h[i] = (alpha / np.sqrt(2.0)) * (
                (1.0 + 2.0 / np.pi) * np.sin(np.pi / (4.0 * alpha))
                + (1.0 - 2.0 / np.pi) * np.cos(np.pi / (4.0 * alpha))
            )
        else:
            num = (np.sin(np.pi * ti * (1.0 - alpha))
                   + 4.0 * alpha * ti * np.cos(np.pi * ti * (1.0 + alpha)))
            den = np.pi * ti * (1.0 - (4.0 * alpha * ti) ** 2)
            h[i] = num / den
    h /= np.sqrt(np.sum(h ** 2))
    return h


_SRRC = _make_srrc(SPS)
_SRRC_DELAY = len(_SRRC) // 2   # group delay in samples


# ---------------------------------------------------------------------------
# DPSK Modulator (single carrier)
# ---------------------------------------------------------------------------

def dpsk_modulate(bits: np.ndarray, m: int, fc: float, fs: int = FS) -> np.ndarray:
    """Modulate `bits` as M-DPSK onto carrier `fc`.

    bits: length must be divisible by log2(m)
    Returns float64 audio samples of length = (n_syms + preamble) * SPS + filter_delay.
    """
    bps = int(np.log2(m))
    assert len(bits) % bps == 0, f"bits len {len(bits)} not divisible by bps={bps}"
    n_syms = len(bits) // bps

    # Map bits → Gray-decoded symbol indices → differential phase increments
    bits = bits.reshape(n_syms, bps)
    indices = np.zeros(n_syms, dtype=np.int32)
    for b in range(bps):
        indices = (indices << 1) | bits[:, b].astype(np.int32)
    indices = _GRAY_DEC[m][indices]          # Gray decode
    delta_phi = indices * (2 * np.pi / m)   # differential phase increments

    # Accumulate phase (differential: each symbol adds its delta)
    phase = np.cumsum(delta_phi)

    # Prepend preamble: 5 known symbols
    pre_phase = np.cumsum(_PREAMBLE_PHASES)
    all_phase = np.concatenate([pre_phase, pre_phase[-1] + phase])
    n_total = PREAMBLE_SYMS + n_syms

    # Generate symbol-rate complex phasors
    phasors = np.exp(1j * all_phase)

    # Upsample: rectangular pulse then SRRC filter
    upsampled = np.zeros(n_total * SPS + len(_SRRC) - 1, dtype=complex)
    for k in range(n_total):
        upsampled[k * SPS] = phasors[k]
    # Convolve with SRRC (matched filter at transmitter)
    baseband = np.convolve(upsampled, _SRRC)[:n_total * SPS + len(_SRRC) - 1]

    # Modulate onto carrier
    t = np.arange(len(baseband)) / fs
    audio = np.real(baseband * np.exp(2j * np.pi * fc * t))
    return audio


# ---------------------------------------------------------------------------
# DPSK Demodulator (single carrier) → soft bits
# ---------------------------------------------------------------------------

def dpsk_demodulate_soft(audio: np.ndarray, m: int, fc: float,
                          n_syms: int, fs: int = FS) -> np.ndarray:
    """Differential coherent demodulation of M-DPSK carrier.

    Returns soft bits: array length = n_syms * log2(m).  Positive means bit=0,
    negative means bit=1.
    """
    bps = int(np.log2(m))

    # Mix to baseband
    t = np.arange(len(audio)) / fs
    bb = audio * np.exp(-2j * np.pi * fc * t)

    # Matched filter
    filtered = np.convolve(bb, _SRRC)

    # Total symbols in stream: preamble + data
    total_syms = PREAMBLE_SYMS + n_syms

    # TX and RX each apply one SRRC, so total group delay = 2 * _SRRC_DELAY
    total_delay = 2 * _SRRC_DELAY
    syms = np.array([
        filtered[min(k * SPS + total_delay, len(filtered) - 1)]
        for k in range(total_syms)
    ])

    # Differential detection: angle(z_k * conj(z_{k-1}))
    diff_angles = np.angle(syms[1:] * np.conj(syms[:-1]))

    # Data angles: the first data diff is syms[PREAMBLE_SYMS] vs syms[PREAMBLE_SYMS-1]
    # i.e., diff_angles[PREAMBLE_SYMS - 1]; slice gives exactly n_syms values.
    data_angles = diff_angles[PREAMBLE_SYMS - 1:PREAMBLE_SYMS - 1 + n_syms]

    # Map differential phase to Gray-coded symbol index
    phase_step = 2 * np.pi / m
    float_idx = (data_angles / phase_step) % m          # raw float index
    hard_idx  = np.round(float_idx).astype(np.int32) % m
    gray_idx  = _GRAY_ENC[m][hard_idx]                  # Gray encode (→ bit pattern)

    # Soft bit values: for each bit position, compute LLR-like value
    # Phase error from nearest constellation point
    phase_err = data_angles - hard_idx * phase_step
    # Confidence ~ 1/phase_error magnitude
    confidence = np.maximum(1.0, 1.0 / (np.abs(phase_err) + 1e-6))

    soft = np.zeros(n_syms * bps, dtype=np.float64)
    for k in range(n_syms):
        for b in range(bps):
            bit_val = (gray_idx[k] >> (bps - 1 - b)) & 1
            sign = -1.0 if bit_val else 1.0
            soft[k * bps + b] = sign * confidence[k]

    return soft


# ---------------------------------------------------------------------------
# Frame codec (FEC + framing around user data)
# ---------------------------------------------------------------------------

class Pactor2Frame:
    """Build and parse PACTOR-II data bursts with conv FEC and CRC-16.

    Packet on-wire (before FEC): [data (N bytes)] + [status (1)] + [CRC-16 (2)]
    After r=1/2 conv encoding the bit stream is interleaved across the two
    carriers: even-indexed encoded bits → carrier 0, odd → carrier 1.
    """

    STATUS_SPEED  = 0x03   # bits 1-0: speed level 1-4
    STATUS_QRT    = 0x04   # bit 2: disconnect request
    STATUS_BKIN   = 0x08   # bit 3: break-in request (IRS wants to become ISS)
    STATUS_HUFFMAN = 0x10  # bit 4: data is PMC/Huffman compressed

    def __init__(self, level: int = 2) -> None:
        assert 1 <= level <= 4
        self.level = level
        self._m   = M_STATES[level]
        self._bps = BITS_PER_SYM[level]
        self._pkt_bytes = _PKT_BYTES[level]
        self._data_bytes = DATA_BYTES[level]

    # -- encode ---------------------------------------------------------------

    def encode(self, data: bytes, status: int = 0) -> tuple[np.ndarray, np.ndarray]:
        """Encode data + status → (carrier0_bits, carrier1_bits).

        Pads/truncates data to self._data_bytes.
        Returns two arrays of length DATA_SYMS * _bps each.
        """
        # Pad or truncate to data_bytes
        db = self._data_bytes
        if len(data) > db:
            data = data[:db]
        data = data + bytes(db - len(data))

        # Build packet: data + status byte + CRC-16 (little-endian, matching crc_check)
        payload = data + bytes([status])
        crc = crc_ccitt(payload)
        pkt = payload + struct.pack("<H", crc)
        assert len(pkt) == self._pkt_bytes, f"pkt {len(pkt)} != {self._pkt_bytes}"

        # Convolutional FEC r=1/2
        info_bits = np.unpackbits(np.frombuffer(pkt, dtype=np.uint8))
        enc = ConvEncoder()
        channel_bits = enc.encode(info_bits)

        # Add 6 termination bits (flush encoder state: 6 × 2 = 12 channel bits)
        flush_bits = enc.encode(np.zeros(6, dtype=np.uint8))
        channel_bits = np.concatenate([channel_bits, flush_bits])

        # Trim/pad to exact DATA_SYMS * 2 * _bps
        target = DATA_SYMS * 2 * self._bps
        if len(channel_bits) > target:
            channel_bits = channel_bits[:target]
        elif len(channel_bits) < target:
            channel_bits = np.pad(channel_bits, (0, target - len(channel_bits)))

        # Interleave across two carriers: even → c0, odd → c1
        c0_bits = channel_bits[0::2]   # length = DATA_SYMS * _bps
        c1_bits = channel_bits[1::2]

        return c0_bits, c1_bits

    # -- decode ---------------------------------------------------------------

    def decode(self, soft0: np.ndarray, soft1: np.ndarray) -> Optional[dict]:
        """Decode soft bits from both carriers → dict or None if CRC fails.

        soft0, soft1: soft bit arrays of length DATA_SYMS * _bps each.
        dict keys: 'data' (bytes), 'status' (int), 'level' (int).
        """
        # De-interleave: reconstruct channel bit stream
        channel_len = len(soft0) + len(soft1)
        soft = np.empty(channel_len, dtype=np.float64)
        soft[0::2] = soft0
        soft[1::2] = soft1

        # Viterbi decode
        dec = ViterbiDecoder(traceback=self._pkt_bytes * 8)
        decoded_bits = dec.decode(soft)
        # terminated=False: flush bits may be < 6 zeros (trim leaves partial flush)
        decoded_bits = np.concatenate([decoded_bits, dec.flush(terminated=False)])

        n_info = self._pkt_bytes * 8
        if len(decoded_bits) < n_info:
            return None
        info_bits = decoded_bits[:n_info].astype(np.uint8)
        pkt = np.packbits(info_bits).tobytes()

        if len(pkt) < self._pkt_bytes:
            return None

        # Verify CRC
        if not crc_check(pkt[:self._pkt_bytes]):
            return None

        payload = pkt[:self._pkt_bytes - 2]
        data    = payload[:self._data_bytes]
        status  = payload[self._data_bytes]

        return {
            'data':   data.rstrip(b'\x00'),
            'status': status,
            'level':  self.level,
        }


# ---------------------------------------------------------------------------
# Modulator: full two-carrier burst
# ---------------------------------------------------------------------------

class Pactor2Modulator:
    """Produce PACTOR-II audio bursts.

    Usage::

        mod = Pactor2Modulator(fs=8000, level=2)
        audio = mod.burst(b"Hello")  # 0.96 s burst, int16
        cs    = mod.cs_reply(0b101010101010, speed_request=2)  # IRS CS
    """

    def __init__(self, fs: int = FS, level: int = 2,
                 carrier0: float = CARRIER_0, carrier1: float = CARRIER_1) -> None:
        self.fs = fs
        self.level = level
        self.c0 = carrier0
        self.c1 = carrier1
        self._frame = Pactor2Frame(level)
        self._m = M_STATES[level]

    def burst(self, data: bytes, status: int = 0) -> np.ndarray:
        """Encode data to a full ~0.96 s DPSK burst (int16)."""
        c0_bits, c1_bits = self._frame.encode(data, status)
        audio0 = dpsk_modulate(c0_bits, self._m, self.c0, self.fs)
        audio1 = dpsk_modulate(c1_bits, self._m, self.c1, self.fs)

        # Mix the two carriers; trim/pad to BURST_SAMPLES
        n = BURST_SAMPLES
        total = np.zeros(n, dtype=np.float64)
        for a in (audio0, audio1):
            seg = a[:n] if len(a) >= n else np.pad(a, (0, n - len(a)))
            total += seg

        total /= 2.0   # normalise
        return np.clip(total * 16384, -32768, 32767).astype(np.int16)

    def cs_reply(self, cs_bits: int, speed_request: int = 0) -> np.ndarray:
        """Generate a 12-bit CS reply burst at 100 Bd DBPSK on carrier 0.

        cs_bits: 12-bit integer (bit 11 = first transmitted)
        speed_request: 0 = keep speed, 1-4 = request speed change
        """
        bits = np.array([(cs_bits >> (11 - i)) & 1 for i in range(CS_REPLY_BITS)], dtype=np.uint8)
        audio = dpsk_modulate(bits, 2, self.c0, self.fs)
        n = CS_SAMPLES
        seg = audio[:n] if len(audio) >= n else np.pad(audio, (0, n - len(audio)))
        return np.clip(seg * 16384, -32768, 32767).astype(np.int16)

    def idle(self, duration: float) -> np.ndarray:
        return np.zeros(int(self.fs * duration), dtype=np.int16)


# ---------------------------------------------------------------------------
# Demodulator: two-carrier burst → soft bits → frame decode
# ---------------------------------------------------------------------------

class Pactor2Demodulator:
    """Demodulate a received PACTOR-II burst.

    Usage::

        demod = Pactor2Demodulator(fs=8000, level=2)
        result = demod.feed_burst(audio)   # returns dict or None
    """

    def __init__(self, fs: int = FS, level: int = 2,
                 carrier0: float = CARRIER_0, carrier1: float = CARRIER_1) -> None:
        self.fs = fs
        self.level = level
        self.c0 = carrier0
        self.c1 = carrier1
        self._frame = Pactor2Frame(level)
        self._m = M_STATES[level]
        self._bps = BITS_PER_SYM[level]

    def feed_burst(self, audio: np.ndarray) -> Optional[dict]:
        """Demodulate one data burst; return decoded frame dict or None."""
        x = audio.astype(np.float64)
        n_data = DATA_SYMS * self._bps
        soft0 = dpsk_demodulate_soft(x, self._m, self.c0, DATA_SYMS, self.fs)
        soft1 = dpsk_demodulate_soft(x, self._m, self.c1, DATA_SYMS, self.fs)
        return self._frame.decode(soft0[:n_data], soft1[:n_data])


# ---------------------------------------------------------------------------
# Memory ARQ accumulator (extend MemoryArq from pactor/arq.py for soft DPSK)
# ---------------------------------------------------------------------------

class Pactor2MemArq:
    """Accumulate soft bits across multiple retransmissions of the same packet.

    After each retransmission, `add()` the soft bit arrays; `try_decode()`
    returns the decoded frame if the sum passes CRC, else None.
    """

    def __init__(self, level: int = 2) -> None:
        self.level = level
        self._frame = Pactor2Frame(level)
        self._bps = BITS_PER_SYM[level]
        n = DATA_SYMS * self._bps
        self._acc0 = np.zeros(n, dtype=np.float64)
        self._acc1 = np.zeros(n, dtype=np.float64)
        self.count = 0

    def add(self, soft0: np.ndarray, soft1: np.ndarray) -> None:
        n = len(self._acc0)
        self._acc0 += soft0[:n]
        self._acc1 += soft1[:n]
        self.count += 1

    def try_decode(self) -> Optional[dict]:
        return self._frame.decode(self._acc0, self._acc1)

    def reset(self) -> None:
        self._acc0[:] = 0
        self._acc1[:] = 0
        self.count = 0


# ---------------------------------------------------------------------------
# Offline ARQ session (for testing; models the ISS ↔ IRS exchange)
# ---------------------------------------------------------------------------

@dataclass
class Pactor2Session:
    """Simplified offline PACTOR-II ARQ session for round-trip tests.

    The session does not drive real audio I/O.  Call pump_cycle() to advance
    one ARQ cycle: ISS encodes and 'sends' a burst; IRS receives and ACKs.
    """

    local_call:  str
    remote_call: str = ''
    level:       int = 2
    max_retries: int = 8

    _mod:   Pactor2Modulator  = field(init=False)
    _demod: Pactor2Demodulator = field(init=False)
    _arq:   Pactor2MemArq     = field(init=False)
    _queue: bytearray          = field(default_factory=bytearray)
    received: bytearray        = field(default_factory=bytearray)
    outgoing_audio: list       = field(default_factory=list)

    def __post_init__(self) -> None:
        self._mod   = Pactor2Modulator(level=self.level)
        self._demod = Pactor2Demodulator(level=self.level)
        self._arq   = Pactor2MemArq(level=self.level)

    def send(self, data: bytes) -> None:
        self._queue.extend(data)

    def pump_cycle(self) -> bool:
        """Advance one ARQ cycle.  Returns True if remote side decoded OK."""
        db = DATA_BYTES[self.level]
        chunk = bytes(self._queue[:db])
        if len(self._queue) >= db:
            del self._queue[:db]

        for attempt in range(self.max_retries):
            audio = self._mod.burst(chunk)
            # Simulate channel (no impairment for offline test)
            result = self._demod.feed_burst(audio)
            if result is not None:
                self.received.extend(result['data'])
                return True
        return False

    def flush(self) -> bytes:
        """Return and clear accumulated received data."""
        out = bytes(self.received)
        self.received.clear()
        return out


# ---------------------------------------------------------------------------
# Top-level TNC interface
# ---------------------------------------------------------------------------

class Pactor2Tnc:
    """Single-object encode/decode interface (mirrors Clover2Tnc).

    Usage::

        tnc = Pactor2Tnc(level=2)
        audio = tnc.transmit(b"CQ CQ CQ DE W1AW")
        data  = tnc.feed(audio)
    """

    def __init__(self, fs: int = FS, level: int = 2,
                 carrier0: float = CARRIER_0,
                 carrier1: float = CARRIER_1) -> None:
        self._mod   = Pactor2Modulator(fs=fs, level=level, carrier0=carrier0, carrier1=carrier1)
        self._demod = Pactor2Demodulator(fs=fs, level=level, carrier0=carrier0, carrier1=carrier1)
        self._level = level

    def transmit(self, data: bytes) -> np.ndarray:
        """Encode data to a PACTOR-II burst (int16 audio)."""
        return self._mod.burst(data)

    def feed(self, audio: np.ndarray) -> bytes:
        """Demodulate a burst; return recovered data or b'' on failure."""
        result = self._demod.feed_burst(audio)
        if result is None:
            return b''
        return result['data']

    @property
    def data_bytes_per_burst(self) -> int:
        return DATA_BYTES[self._level]
