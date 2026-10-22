"""AX.25 HDLC line coding: FCS-16, bit stuffing, flags and NRZI.

AX.25 sends each octet LSB first, inserts a zero after five consecutive ones, delimits frames with 0x7E flags and
NRZI-codes the result (a 0 bit is a change of tone, a 1 bit is no change). The FCS is the X.25 CRC-CCITT, sent low
byte first. The receiver and ``crc16_hdlc`` are slimmed from El-Modem (``v42_lapm._Hdlc``, ``mnp_link.crc16_hdlc``).
"""

from __future__ import annotations

FLAG = 0x7E
# Duplicate suppression: a frame decoded twice (plain + FX.25 path, slicer bank, --repeat copies) is reported
# once if both copies fall in the same burst (DCD rising edge to rising edge) or within DEDUPE_NEAR_BITS of each
# other. Protocol retransmissions (SABM/DISC/RR retries after T1) always come in a new burst, so they get through
# -- a pure time window long enough for 300 bd FX.25 swallowed them and stalled connected mode.
DEDUPE_NEAR_BITS = 2048


def track_burst(dem) -> None:
    """Count DCD rising edges on a demodulator (call once per input block)."""
    busy = dem.dcd
    if busy and not getattr(dem, "_was_busy", False):
        dem._burst = getattr(dem, "_burst", 0) + 1
    dem._was_busy = busy
FLAG_BITS = (0, 1, 1, 1, 1, 1, 1, 0)
MIN_FRAME = 15                             # two 7-octet addresses + control (FCS not counted)
MAX_FRAME = 2048


def _crc_table() -> list[int]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_CRC_TABLE = _crc_table()


def crc16_hdlc(data: bytes) -> int:
    """X.25 FCS: reflected 0x1021, initial 0xFFFF, complemented (table-driven)."""
    crc = 0xFFFF
    t = _CRC_TABLE
    for byte in data:
        crc = (crc >> 8) ^ t[(crc ^ byte) & 0xFF]
    return crc ^ 0xFFFF


def stuffed_bits(frame: bytes) -> list[int]:
    """Frame body plus FCS as line bits, LSB first, with zero insertion (no flags)."""
    body = frame + crc16_hdlc(frame).to_bytes(2, "little")
    bits: list[int] = []
    ones = 0
    for byte in body:
        for i in range(8):
            b = (byte >> i) & 1
            bits.append(b)
            ones = ones + 1 if b else 0
            if ones == 5:
                bits.append(0)
                ones = 0
    return bits


def frame_bits(frame: bytes, lead_flags: int, tail_flags: int = 2) -> list[int]:
    """One transmission: ``lead_flags`` flags (TXDELAY), the stuffed frame, ``tail_flags`` flags (TXTAIL)."""
    return list(FLAG_BITS) * lead_flags + stuffed_bits(frame) + list(FLAG_BITS) * max(tail_flags, 1)


def burst_bits(frames: list[bytes], lead_flags: int, tail_flags: int = 2) -> list[int]:
    """Several frames in one key-up, separated by single shared flags."""
    bits = list(FLAG_BITS) * lead_flags
    for frame in frames:
        bits += stuffed_bits(frame) + list(FLAG_BITS)
    return bits + list(FLAG_BITS) * max(tail_flags - 1, 0)


class NrziEncoder:
    """0 toggles the line level, 1 holds it. The level persists across bursts like a real modulator's would."""

    def __init__(self) -> None:
        self.level = 1

    def encode(self, bits: list[int]) -> list[int]:
        out = []
        level = self.level
        for b in bits:
            if not b:
                level ^= 1
            out.append(level)
        self.level = level
        return out


class NrziDecoder:
    def __init__(self) -> None:
        self.level = 1

    def decode(self, level: int) -> int:
        bit = 1 if level == self.level else 0
        self.level = level
        return bit


class HdlcDecoder:
    """Bit-level HDLC receiver: flag hunt, zero deletion, FCS check.

    ``push`` returns a frame (without FCS) when a closing flag ends a frame whose FCS is good, else None.
    ``bad_fcs`` counts octet-aligned frames of plausible length that failed the FCS check.
    """

    def __init__(self) -> None:
        self.ones = 0
        self.bits: list[int] = []
        self.active = False
        self.flags = 0
        self.bad_fcs = 0

    def reset(self) -> None:
        self.ones, self.bits, self.active = 0, [], False

    def push(self, bit: int) -> bytes | None:
        if bit:
            self.ones += 1
            if self.active:
                self.bits.append(1)
                if len(self.bits) > (MAX_FRAME + 3) * 8:
                    self.active, self.bits = False, []
            if self.ones >= 7:                 # abort / idle
                self.active, self.bits = False, []
            return None
        ones, self.ones = self.ones, 0
        if ones == 5:
            return None                        # stuffed zero
        if ones == 6:                          # flag: its leading zero and six ones are already stored
            self.flags += 1
            out = None
            if self.active:
                data = self.bits[:-7]
                if len(data) % 8 == 0 and len(data) >= (MIN_FRAME + 2) * 8:
                    raw = bytes(sum(data[i + j] << j for j in range(8)) for i in range(0, len(data), 8))
                    if crc16_hdlc(raw[:-2]) == int.from_bytes(raw[-2:], "little"):
                        out = raw[:-2]
                    else:
                        self.bad_fcs += 1
            self.active, self.bits = True, []
            return out
        if self.active:
            self.bits.append(0)
        return None

    # -------------------------------------------------------------- vectorised path (for the fast modes)

    def push_bits(self, bits) -> list[bytes]:
        """The same decoder over a whole block of bits at once (numpy): flags by 8-bit window match, aborts
        (>= 7 ones) and stuffed zeros (after exactly five ones) from run lengths. Don't mix with ``push`` on
        one instance."""
        import numpy as np  # noqa: PLC0415
        pend = getattr(self, "_pend", None)
        if pend is None:
            pend = self._pend = np.zeros(0, np.uint8)
            self._open = False
        ext = np.concatenate((pend, np.asarray(bits, np.uint8)))
        out: list[bytes] = []
        if len(ext) < 8:
            self._pend = ext
            return out
        win = np.lib.stride_tricks.sliding_window_view(ext, 8)
        starts = np.flatnonzero(np.packbits(win, axis=1, bitorder="little")[:, 0] == FLAG)
        # An open frame's pending bits begin with its opening flag, found again here at index 0.
        self.flags += len(starts) - (1 if self._open and len(starts) else 0)
        for a, b in zip(starts[:-1], starts[1:]):
            if b > a + 8:                                # adjacent / shared-zero flags enclose nothing
                frame = self._segment(ext[a + 8:b], np)
                if frame is not None:
                    out.append(frame)
        if len(starts):
            keep, self._open = ext[starts[-1]:], True
        else:
            keep, self._open = ext[-7:], False
        if len(keep) > (MAX_FRAME + 4) * 8:              # runaway "frame": give up on it
            keep, self._open = ext[-7:], False
        self._pend = keep
        return out

    def _segment(self, seg, np) -> bytes | None:
        n = len(seg)
        if n < (MIN_FRAME + 2) * 8:
            return None
        idx = np.arange(n)
        zero = seg == 0
        lastz = np.maximum.accumulate(np.where(zero, idx, -1))
        run_end = idx - lastz                           # ones ending at i (inclusive)
        if run_end.max() >= 7:
            return None                                 # abort inside the frame
        before = np.concatenate(([0], run_end[:-1]))    # ones immediately before i
        data = seg[~(zero & (before == 5))]
        if len(data) % 8:
            return None
        raw = np.packbits(data, bitorder="little").tobytes()
        if len(raw) < MIN_FRAME + 2:
            return None
        if crc16_hdlc(raw[:-2]) == int.from_bytes(raw[-2:], "little"):
            return raw[:-2]
        self.bad_fcs += 1
        return None


# ------------------------------------------------------------------ erasure tracking (for FX.25)
#
# When the audio leg conceals a lost packet, the demodulator still produces bits for that stretch -- plausible
# but wrong. Marking them lets the FX.25 decoder treat the bytes as erasures (twice the repair reach of unknown
# errors). Demodulators keep the concealed spans on their own sample clock (widened by their filters' length)
# and carry the mark forward in the bit domain over NRZI and descrambler memory.


def span_mask(spans: list[tuple[int, int]], start: int, n: int):
    """Boolean mask for samples [start, start + n) lying inside any (begin, end) span."""
    import numpy as np  # noqa: PLC0415 -- keep this module importable without numpy for the codec-only users
    mask = np.zeros(n, bool)
    for b, e in spans:
        lo, hi = max(b - start, 0), min(e - start, n)
        if lo < hi:
            mask[lo:hi] = True
    return mask


def extend_bits(mask, since: int, tail: int):
    """Mark each bit within ``tail`` bits after an erased bit too. ``since``: bits since the last erased bit at
    the end of the previous call. Returns (extended mask, new since)."""
    import numpy as np  # noqa: PLC0415
    n = len(mask)
    if n == 0:
        return np.zeros(0, bool), since
    idx = np.arange(n)
    last = np.maximum.accumulate(np.where(mask, idx, -since - 1))
    return (idx - last) <= tail, int(n - 1 - last[-1])
