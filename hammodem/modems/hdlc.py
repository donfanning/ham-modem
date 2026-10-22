"""AX.25 HDLC line coding: FCS-16, bit stuffing, flags and NRZI.

AX.25 sends each octet LSB first, inserts a zero after five consecutive ones, delimits frames with 0x7E flags and
NRZI-codes the result (a 0 bit is a change of tone, a 1 bit is no change). The FCS is the X.25 CRC-CCITT, sent low
byte first. The receiver and ``crc16_hdlc`` are slimmed from El-Modem (``v42_lapm._Hdlc``, ``mnp_link.crc16_hdlc``).
"""

from __future__ import annotations

FLAG = 0x7E
FLAG_BITS = (0, 1, 1, 1, 1, 1, 1, 0)
MIN_FRAME = 15                             # two 7-octet addresses + control (FCS not counted)
MAX_FRAME = 2048


def crc16_hdlc(data: bytes) -> int:
    """X.25 FCS: reflected 0x1021, initial 0xFFFF, complemented."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
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
