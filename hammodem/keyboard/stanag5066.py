"""STANAG 5066 data-link layer for HF serial-tone modems.

Provides reliable (ARQ) and unreliable (non-ARQ) data transfer over
STANAG 4285 or MIL-STD-188-110 waveforms.  The protocol sits between the
application (S_Primitives) and the physical modem, adding framing,
segmentation, CRC-32, addressing and selective-repeat ARQ.

Reference: NATO STANAG 5066 Ed. 3 (2018), publicly available through ISODE
and ACP 142 implementations.
"""

from __future__ import annotations

import struct
import time
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYNC_WORD = bytes([0x90, 0xEB])  # D_PDU sync: 0x90EB
MAX_SEGMENT = 1023               # max data segment size (10-bit field)
MAX_ADDRESS_LEN = 7              # packed address: up to 7 bytes
CRC_POLY = 0x04C11DB7            # CRC-32 CCITT
HEADER_SIZE = 16                 # fixed D_PDU header (including sync + CRC)
MAX_RETRIES = 5
ACK_TIMEOUT = 10.0               # seconds (HF typical)


class DPduType(IntEnum):
    DATA_ONLY = 0
    ACK_ONLY = 1
    DATA_ACK = 2
    RESET_WIN_RESYNC = 3
    EXP_DATA_ONLY = 4
    EXP_ACK_ONLY = 5
    MANAGEMENT = 6
    NON_ARQ_DATA = 7
    EXP_NON_ARQ_DATA = 8
    WARNING = 15


class ServiceType(IntEnum):
    ARQ = 0
    NON_ARQ = 1
    NON_ARQ_NO_ERR = 2


# ---------------------------------------------------------------------------
# CRC-32
# ---------------------------------------------------------------------------

_CRC_TABLE: list[int] | None = None


def _build_crc_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ CRC_POLY) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
        table.append(crc)
    return table


def crc32_5066(data: bytes | bytearray) -> int:
    global _CRC_TABLE
    if _CRC_TABLE is None:
        _CRC_TABLE = _build_crc_table()
    crc = 0xFFFFFFFF
    for b in data:
        idx = ((crc >> 24) ^ b) & 0xFF
        crc = ((_CRC_TABLE[idx] ^ (crc << 8)) & 0xFFFFFFFF)
    return crc ^ 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Address encoding (packed 4-bit per character, padded to 7 bytes)
# ---------------------------------------------------------------------------

_ADDR_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ "
_CHAR_TO_VAL = {c: i for i, c in enumerate(_ADDR_CHARS)}


def encode_address(callsign: str) -> bytes:
    """Pack a callsign into 7 bytes using 6-bit-per-character encoding."""
    callsign = callsign.upper().ljust(9)[:9]
    vals = [_CHAR_TO_VAL.get(c, 36) for c in callsign]
    bits = 0
    for v in vals:
        bits = (bits << 6) | (v & 0x3F)
    bits <<= (56 - 54)  # 9 chars * 6 bits = 54, pad to 56 bits (7 bytes)
    out = bytearray(7)
    for i in range(7):
        out[i] = (bits >> (48 - i * 8)) & 0xFF
    return bytes(out)


def decode_address(raw: bytes) -> str:
    """Unpack a 7-byte encoded address back to a callsign."""
    bits = 0
    for b in raw[:7]:
        bits = (bits << 8) | b
    bits >>= 2  # remove the 2-bit pad
    chars = []
    for _ in range(9):
        v = bits & 0x3F
        chars.append(_ADDR_CHARS[v] if v < len(_ADDR_CHARS) else '?')
        bits >>= 6
    return "".join(reversed(chars)).rstrip()


# ---------------------------------------------------------------------------
# D_PDU (Data-link Protocol Data Unit)
# ---------------------------------------------------------------------------

@dataclass
class DPdu:
    pdu_type: DPduType
    eow: int = 0            # engineering orderwire (4 bits)
    eot: bool = False        # end of transmission
    src: bytes = b'\x00' * 7
    dst: bytes = b'\x00' * 7
    size: int = 0            # segment size (10 bits)
    tx_seq: int = 0          # transmit sequence number (8 bits)
    rx_seq: int = 0          # receive sequence number (8 bits for ACK)
    data: bytes = b""

    def encode(self) -> bytes:
        hdr = bytearray(HEADER_SIZE)
        hdr[0:2] = SYNC_WORD
        kind = (self.pdu_type & 0x0F) | ((self.eow & 0x0F) << 4)
        hdr[2] = kind
        flags = (1 if self.eot else 0) | ((self.size >> 8) & 0x03) << 1
        hdr[3] = flags
        hdr[4] = self.size & 0xFF
        hdr[5] = self.tx_seq
        hdr[6] = self.rx_seq
        hdr[7:14] = self.src[:7]
        # CRC covers header bytes 2..15 (excluding sync and CRC itself) + data
        payload = bytes(hdr[2:14]) + self.data
        crc = crc32_5066(payload)
        hdr[14] = (crc >> 8) & 0xFF
        hdr[15] = crc & 0xFF
        return bytes(hdr) + self.data

    @classmethod
    def decode(cls, raw: bytes) -> "DPdu | None":
        if len(raw) < HEADER_SIZE:
            return None
        if raw[0:2] != SYNC_WORD:
            return None
        kind = raw[2]
        pdu_type = DPduType(kind & 0x0F)
        eow = (kind >> 4) & 0x0F
        flags = raw[3]
        eot = bool(flags & 1)
        size = ((flags >> 1) & 0x03) << 8 | raw[4]
        tx_seq = raw[5]
        rx_seq = raw[6]
        src = raw[7:14]
        data = raw[HEADER_SIZE:HEADER_SIZE + size]
        payload = raw[2:14] + data
        expected_crc = crc32_5066(payload)
        got_crc = (raw[14] << 8) | raw[15]
        if (expected_crc & 0xFFFF) != got_crc:
            return None
        return cls(pdu_type=pdu_type, eow=eow, eot=eot, src=src, dst=b'\x00' * 7,
                   size=size, tx_seq=tx_seq, rx_seq=rx_seq, data=data)


# ---------------------------------------------------------------------------
# Segmentation / reassembly
# ---------------------------------------------------------------------------

def segment(data: bytes, max_seg: int = MAX_SEGMENT) -> list[bytes]:
    if not data:
        return [b""]
    return [data[i:i + max_seg] for i in range(0, len(data), max_seg)]


def reassemble(segments: dict[int, bytes], count: int) -> bytes:
    return b"".join(segments.get(i, b"") for i in range(count))


# ---------------------------------------------------------------------------
# S_Primitive interface (application <-> data-link)
# ---------------------------------------------------------------------------

@dataclass
class SPrimitive:
    kind: str           # "BIND", "UNBIND", "DATA", "DATA_IND", "UDATA", "UDATA_IND"
    src: str = ""
    dst: str = ""
    data: bytes = b""
    service: ServiceType = ServiceType.ARQ
    priority: int = 0


# ---------------------------------------------------------------------------
# ARQ state machine (selective repeat, single session)
# ---------------------------------------------------------------------------

@dataclass
class _TxEntry:
    seq: int
    data: bytes
    sent_at: float = 0.0
    retries: int = 0
    acked: bool = False


@dataclass
class DataLinkSession:
    """One STANAG 5066 data-link session between two stations."""

    local_addr: str
    remote_addr: str = ""
    _tx_seq: int = 0
    _rx_seq: int = 0
    _tx_window: dict = field(default_factory=dict)
    _rx_buffer: dict = field(default_factory=dict)
    _rx_expected: int = 0
    _delivered: bytearray = field(default_factory=bytearray)
    _tx_queue: deque = field(default_factory=deque)
    outgoing_pdus: list = field(default_factory=list)
    incoming_data: list = field(default_factory=list)
    connected: bool = False

    def bind(self, remote: str) -> None:
        self.remote_addr = remote
        self.connected = True
        self._tx_seq = 0
        self._rx_seq = 0
        self._tx_window.clear()
        self._rx_buffer.clear()
        self._rx_expected = 0

    def unbind(self) -> None:
        self.connected = False
        self.remote_addr = ""

    def send(self, data: bytes) -> None:
        for seg in segment(data):
            self._tx_queue.append(seg)

    def send_non_arq(self, data: bytes) -> None:
        src = encode_address(self.local_addr)
        dst = encode_address(self.remote_addr)
        for seg in segment(data):
            pdu = DPdu(pdu_type=DPduType.NON_ARQ_DATA, src=src, dst=dst,
                       size=len(seg), data=seg, eot=(seg is data[-len(seg):]))
            self.outgoing_pdus.append(pdu.encode())

    def pump(self) -> None:
        """Advance the TX side: send queued segments, retransmit timed-out ones."""
        now = time.monotonic()
        src = encode_address(self.local_addr)
        while self._tx_queue and len(self._tx_window) < 8:
            seg = self._tx_queue.popleft()
            seq = self._tx_seq & 0xFF
            entry = _TxEntry(seq=seq, data=seg, sent_at=now)
            self._tx_window[seq] = entry
            is_last = not self._tx_queue
            pdu = DPdu(pdu_type=DPduType.DATA_ONLY, src=src, size=len(seg),
                       tx_seq=seq, data=seg, eot=is_last)
            self.outgoing_pdus.append(pdu.encode())
            self._tx_seq = (self._tx_seq + 1) & 0xFF
        for entry in list(self._tx_window.values()):
            if not entry.acked and now - entry.sent_at > ACK_TIMEOUT:
                if entry.retries >= MAX_RETRIES:
                    del self._tx_window[entry.seq]
                    continue
                entry.retries += 1
                entry.sent_at = now
                pdu = DPdu(pdu_type=DPduType.DATA_ONLY, src=src, size=len(entry.data),
                           tx_seq=entry.seq, data=entry.data)
                self.outgoing_pdus.append(pdu.encode())

    def receive(self, raw: bytes) -> None:
        """Process a received D_PDU."""
        pdu = DPdu.decode(raw)
        if pdu is None:
            return
        if pdu.pdu_type in (DPduType.DATA_ONLY, DPduType.DATA_ACK, DPduType.EXP_DATA_ONLY):
            self._rx_buffer[pdu.tx_seq] = pdu.data
            while self._rx_expected in self._rx_buffer:
                self._delivered.extend(self._rx_buffer.pop(self._rx_expected))
                self._rx_expected = (self._rx_expected + 1) & 0xFF
            self.incoming_data.append(bytes(self._delivered))
            self._delivered.clear()
            ack = DPdu(pdu_type=DPduType.ACK_ONLY,
                       src=encode_address(self.local_addr),
                       rx_seq=pdu.tx_seq)
            self.outgoing_pdus.append(ack.encode())
        elif pdu.pdu_type in (DPduType.ACK_ONLY, DPduType.DATA_ACK, DPduType.EXP_ACK_ONLY):
            if pdu.rx_seq in self._tx_window:
                self._tx_window[pdu.rx_seq].acked = True
                del self._tx_window[pdu.rx_seq]
        elif pdu.pdu_type == DPduType.NON_ARQ_DATA:
            self.incoming_data.append(pdu.data)

    def pending_acks(self) -> int:
        return sum(1 for e in self._tx_window.values() if not e.acked)
