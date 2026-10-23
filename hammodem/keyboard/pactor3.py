"""PACTOR-III HF modem (levels 1-6, 2-18 carriers, 100 Bd DPSK/DQPSK).

Clean-room implementation from published WAVECOM protocol tables and
over-the-air recordings.  Waveform structure follows the published
carrier table; exact framing details (header layout, code rate for
levels 5-6) are derived from channel capacity rather than vendor specs.

Physical layer:
    18 possible carriers at 120 Hz spacing starting at 480 Hz.
    Each carrier: 100 Bd DPSK/DQPSK, SRRC pulse (alpha=0.5, span=4),
    5-symbol preamble + 91 data symbols per burst.

ARQ timing (same as PACTOR-II):
    Burst  ~0.96 s + filter tail, CS reply ~0.270 s, cycle 1.25 s.
"""

import struct
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from hammodem.pactor.code import crc_ccitt, crc_check
from hammodem.keyboard.milstd import ConvEncoder, ViterbiDecoder
from hammodem.keyboard.pactor2 import (
    FS, BAUD, SPS,
    BURST_SYMS, PREAMBLE_SYMS, DATA_SYMS,
    BURST_SAMPLES, CS_SAMPLES, CYCLE_SAMPLES,
    BURST_S, CS_S, CYCLE_S,
    _SRRC, _SRRC_DELAY,
    dpsk_modulate, dpsk_demodulate_soft,
)

# ---------------------------------------------------------------------------
# Carrier table (WAVECOM published)
# ---------------------------------------------------------------------------

CARRIER_BASE    = 480.0    # Hz, index-0 carrier
CARRIER_SPACING = 120.0    # Hz between adjacent carriers
N_CARRIERS_MAX  = 18

CARRIER_FREQS = np.array(
    [CARRIER_BASE + i * CARRIER_SPACING for i in range(N_CARRIERS_MAX)]
)

# Active carrier indices per speed level (0-based into CARRIER_FREQS)
CARRIER_INDICES: dict[int, list[int]] = {
    1: [5, 12],
    2: [3, 5, 8, 10, 12, 14],
    3: list(range(2, 16)),    # 14 carriers
    4: list(range(2, 16)),    # 14 carriers
    5: list(range(1, 17)),    # 16 carriers
    6: list(range(0, 18)),    # 18 carriers
}

N_CARRIERS: dict[int, int] = {k: len(v) for k, v in CARRIER_INDICES.items()}

# Modulation per level: DBPSK (m=2) for 1-3, DQPSK (m=4) for 4-6
M_PER_LEVEL   = {1: 2, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4}
BPS_PER_LEVEL = {1: 1, 2: 1, 3: 1, 4: 2, 5: 2, 6: 2}

# Channel bits available per burst (all carriers combined)
_CHANNEL_BITS = {
    k: N_CARRIERS[k] * DATA_SYMS * BPS_PER_LEVEL[k]
    for k in range(1, 7)
}
# Info bits after r=1/2 FEC
_INFO_BITS = {k: _CHANNEL_BITS[k] // 2 for k in range(1, 7)}
# Packet bytes
_PKT_BYTES = {k: _INFO_BITS[k] // 8 for k in range(1, 7)}
# Overhead: 1 status byte + 2 CRC bytes
_OVERHEAD = 3
# Net user-data bytes per burst
DATA_BYTES: dict[int, int] = {
    k: max(0, _PKT_BYTES[k] - _OVERHEAD) for k in range(1, 7)
}

CS_REPLY_BITS = 12   # same 12-bit control signal as PACTOR-II


# ---------------------------------------------------------------------------
# Frame encoder / decoder
# ---------------------------------------------------------------------------

class Pactor3Frame:
    """Encode/decode a single PACTOR-III data burst.

    Packet on-wire (before FEC):
        [data (N bytes)] + [status (1 byte)] + [CRC-16 (2 bytes)]

    After r=1/2 conv FEC, bits are round-robin interleaved across
    the active carriers (bit i → carrier i % n_carriers).
    Each carrier carries DATA_SYMS * bps bits.
    """

    STATUS_SPEED   = 0x07   # bits 2-0: speed level 1-6
    STATUS_QRT     = 0x08   # bit 3: disconnect request
    STATUS_BKIN    = 0x10   # bit 4: break-in

    def __init__(self, level: int = 2) -> None:
        assert 1 <= level <= 6
        self.level      = level
        self._m         = M_PER_LEVEL[level]
        self._bps       = BPS_PER_LEVEL[level]
        self._nc        = N_CARRIERS[level]
        self._pkt_bytes = _PKT_BYTES[level]
        self._data_bytes = DATA_BYTES[level]
        self._cidx      = CARRIER_INDICES[level]

    # -- encode ---------------------------------------------------------------

    def encode(self, data: bytes,
               status: int = 0) -> list[np.ndarray]:
        """Encode data + status → list of per-carrier bit arrays.

        Returns a list of length N_CARRIERS[level], each array of length
        DATA_SYMS * BPS_PER_LEVEL[level].
        """
        db = self._data_bytes
        data = (data + bytes(db))[:db]   # pad / truncate

        payload = data + bytes([status])
        crc = crc_ccitt(payload)
        pkt = payload + struct.pack("<H", crc)
        assert len(pkt) == self._pkt_bytes

        info_bits = np.unpackbits(np.frombuffer(pkt, dtype=np.uint8))
        enc = ConvEncoder()
        ch = enc.encode(info_bits)

        # Flush: 6 tail zeros (K=7 encoder, 6 memory bits)
        flush = enc.encode(np.zeros(6, dtype=np.uint8))
        ch = np.concatenate([ch, flush])

        # Trim/pad to exact channel capacity
        target = self._nc * DATA_SYMS * self._bps
        if len(ch) > target:
            ch = ch[:target]
        elif len(ch) < target:
            ch = np.pad(ch, (0, target - len(ch)))

        # Round-robin interleave across carriers
        bits_per_carrier = DATA_SYMS * self._bps
        carriers = [ch[i::self._nc][:bits_per_carrier] for i in range(self._nc)]
        # Ensure exact length (pad if necessary due to floor division)
        carriers = [
            np.pad(c, (0, bits_per_carrier - len(c))) if len(c) < bits_per_carrier else c
            for c in carriers
        ]
        return carriers

    # -- decode ---------------------------------------------------------------

    def decode(self, soft_carriers: list[np.ndarray]) -> Optional[dict]:
        """Decode per-carrier soft bits → dict or None on CRC failure.

        soft_carriers: list of length N_CARRIERS[level], each of length
        DATA_SYMS * BPS_PER_LEVEL[level].  Positive = bit 0, negative = bit 1.
        """
        if len(soft_carriers) != self._nc:
            return None

        bits_per_carrier = DATA_SYMS * self._bps
        # De-interleave (reverse of round-robin)
        total = self._nc * bits_per_carrier
        soft_ch = np.empty(total, dtype=np.float64)
        for ci, s in enumerate(soft_carriers):
            n = min(len(s), bits_per_carrier)
            soft_ch[ci:total:self._nc][:n] = s[:n]

        # Viterbi decode
        n_info = self._pkt_bytes * 8
        dec = ViterbiDecoder(traceback=n_info)
        decoded = dec.decode(soft_ch)
        tail = dec.flush(terminated=False)
        full = np.concatenate([decoded, tail])

        if len(full) < n_info:
            return None
        info_bits = full[:n_info].astype(np.uint8)
        pkt = np.packbits(info_bits).tobytes()

        if not crc_check(pkt[:self._pkt_bytes]):
            return None

        payload = pkt[:self._pkt_bytes - 2]
        data   = payload[:self._data_bytes]
        status = payload[self._data_bytes]
        return {'data': data, 'status': status, 'level': self.level}


# ---------------------------------------------------------------------------
# Modulator
# ---------------------------------------------------------------------------

class Pactor3Modulator:
    """Produce PACTOR-III multi-carrier audio bursts.

    Each active carrier is independently DPSK-modulated; they are summed
    and normalised to int16 PCM at 8 kHz.
    """

    def __init__(self, fs: int = FS, level: int = 2) -> None:
        self.fs    = fs
        self.level = level
        self._m    = M_PER_LEVEL[level]
        self._cidx = CARRIER_INDICES[level]
        self._frame = Pactor3Frame(level)

    def burst(self, data: bytes, status: int = 0) -> np.ndarray:
        """Encode data to a multi-carrier DPSK burst (int16)."""
        carrier_bits = self._frame.encode(data, status)
        combined = np.zeros(BURST_SAMPLES, dtype=np.float64)
        for ci, bits in zip(self._cidx, carrier_bits):
            fc = CARRIER_FREQS[ci]
            audio = dpsk_modulate(bits, self._m, fc, self.fs)
            n = BURST_SAMPLES
            seg = audio[:n] if len(audio) >= n else np.pad(audio, (0, n - len(audio)))
            combined += seg
        combined /= len(self._cidx)
        return np.clip(combined * 16384, -32768, 32767).astype(np.int16)

    def cs_reply(self, cs_bits: int) -> np.ndarray:
        """12-bit DBPSK CS reply on carrier index 5 (1080 Hz)."""
        bits = np.array(
            [(cs_bits >> (11 - i)) & 1 for i in range(CS_REPLY_BITS)],
            dtype=np.uint8,
        )
        fc = CARRIER_FREQS[5]
        audio = dpsk_modulate(bits, 2, fc, self.fs)
        n = CS_SAMPLES
        seg = audio[:n] if len(audio) >= n else np.pad(audio, (0, n - len(audio)))
        return np.clip(seg * 16384, -32768, 32767).astype(np.int16)

    def idle(self, duration: float) -> np.ndarray:
        return np.zeros(int(self.fs * duration), dtype=np.int16)


# ---------------------------------------------------------------------------
# Demodulator
# ---------------------------------------------------------------------------

class Pactor3Demodulator:
    """Demodulate a PACTOR-III multi-carrier burst."""

    def __init__(self, fs: int = FS, level: int = 2) -> None:
        self.fs    = fs
        self.level = level
        self._m    = M_PER_LEVEL[level]
        self._bps  = BPS_PER_LEVEL[level]
        self._cidx = CARRIER_INDICES[level]
        self._frame = Pactor3Frame(level)

    def feed_burst(self, audio: np.ndarray) -> Optional[dict]:
        """Demodulate one data burst; return decoded frame dict or None."""
        x = audio.astype(np.float64)
        soft_carriers = [
            dpsk_demodulate_soft(x, self._m, CARRIER_FREQS[ci], DATA_SYMS, self.fs)
            for ci in self._cidx
        ]
        return self._frame.decode(soft_carriers)


# ---------------------------------------------------------------------------
# Memory ARQ (soft-bit accumulation)
# ---------------------------------------------------------------------------

class Pactor3MemArq:
    """Accumulate soft bits across retransmissions for combining gain."""

    def __init__(self, level: int = 2) -> None:
        self.level  = level
        self._frame = Pactor3Frame(level)
        bpc = DATA_SYMS * BPS_PER_LEVEL[level]
        nc  = N_CARRIERS[level]
        self._acc = [np.zeros(bpc, dtype=np.float64) for _ in range(nc)]
        self.count = 0

    def add(self, soft_carriers: list[np.ndarray]) -> None:
        bpc = DATA_SYMS * BPS_PER_LEVEL[self.level]
        for i, s in enumerate(soft_carriers):
            n = min(len(s), bpc)
            self._acc[i][:n] += s[:n]
        self.count += 1

    def try_decode(self) -> Optional[dict]:
        return self._frame.decode(self._acc)

    def reset(self) -> None:
        for a in self._acc:
            a[:] = 0.0
        self.count = 0


# ---------------------------------------------------------------------------
# Offline ARQ session
# ---------------------------------------------------------------------------

@dataclass
class Pactor3Session:
    """Simplified offline PACTOR-III ARQ session for round-trip tests."""

    local_call:  str
    remote_call: str = ''
    level:       int = 2
    max_retries: int = 8

    _mod:    Pactor3Modulator  = field(init=False)
    _demod:  Pactor3Demodulator = field(init=False)
    _arq:    Pactor3MemArq     = field(init=False)
    _queue:  bytearray          = field(default_factory=bytearray)
    received: bytearray         = field(default_factory=bytearray)

    def __post_init__(self) -> None:
        self._mod   = Pactor3Modulator(level=self.level)
        self._demod = Pactor3Demodulator(level=self.level)
        self._arq   = Pactor3MemArq(level=self.level)

    def send(self, data: bytes) -> None:
        self._queue.extend(data)

    def pump_cycle(self) -> bool:
        db = DATA_BYTES[self.level]
        chunk = bytes(self._queue[:db])
        if len(self._queue) >= db:
            del self._queue[:db]
        for _ in range(self.max_retries):
            audio  = self._mod.burst(chunk)
            result = self._demod.feed_burst(audio)
            if result is not None:
                self.received.extend(result['data'])
                return True
        return False

    def flush(self) -> bytes:
        out = bytes(self.received)
        self.received.clear()
        return out


# ---------------------------------------------------------------------------
# Top-level TNC interface
# ---------------------------------------------------------------------------

class Pactor3Tnc:
    """Single-object encode/decode API (mirrors Pactor2Tnc).

    Usage::

        tnc = Pactor3Tnc(level=3)
        audio = tnc.transmit(b"CQ CQ DE W1AW")
        data  = tnc.feed(audio)
    """

    def __init__(self, level: int = 2) -> None:
        self._mod   = Pactor3Modulator(level=level)
        self._demod = Pactor3Demodulator(level=level)

    def transmit(self, data: bytes) -> np.ndarray:
        return self._mod.burst(data)

    def feed(self, audio: np.ndarray) -> bytes:
        result = self._demod.feed_burst(audio)
        if result is None:
            return b''
        return result['data'].rstrip(b'\x00')
