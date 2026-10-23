"""PACTOR-I codec: CRC-CCITT, Huffman table, control signals, status byte, and callsign handling.

Sources: hfterm pactor.c (Thomas Sailer, GPL), ARRL PACTOR page, NB6Z pactor.htm.
The Huffman table is the standard PACTOR-I table from the hfterm source (128 entries, LSB first).
"""

from __future__ import annotations

import numpy as np

# --------------------------------------------------------------------------- CRC

_CRC_TABLE = np.array([
    0x0000, 0x1189, 0x2312, 0x329b, 0x4624, 0x57ad, 0x6536, 0x74bf,
    0x8c48, 0x9dc1, 0xaf5a, 0xbed3, 0xca6c, 0xdbe5, 0xe97e, 0xf8f7,
    0x1081, 0x0108, 0x3393, 0x221a, 0x56a5, 0x472c, 0x75b7, 0x643e,
    0x9cc9, 0x8d40, 0xbfdb, 0xae52, 0xdaed, 0xcb64, 0xf9ff, 0xe876,
    0x2102, 0x308b, 0x0210, 0x1399, 0x6726, 0x76af, 0x4434, 0x55bd,
    0xad4a, 0xbcc3, 0x8e58, 0x9fd1, 0xeb6e, 0xfae7, 0xc87c, 0xd9f5,
    0x3183, 0x200a, 0x1291, 0x0318, 0x77a7, 0x662e, 0x54b5, 0x453c,
    0xbdcb, 0xac42, 0x9ed9, 0x8f50, 0xfbef, 0xea66, 0xd8fd, 0xc974,
    0x4204, 0x538d, 0x6116, 0x709f, 0x0420, 0x15a9, 0x2732, 0x36bb,
    0xce4c, 0xdfc5, 0xed5e, 0xfcd7, 0x8868, 0x99e1, 0xab7a, 0xbaf3,
    0x5285, 0x430c, 0x7197, 0x601e, 0x14a1, 0x0528, 0x37b3, 0x263a,
    0xdecd, 0xcf44, 0xfddf, 0xec56, 0x98e9, 0x8960, 0xbbfb, 0xaa72,
    0x6306, 0x728f, 0x4014, 0x519d, 0x2522, 0x34ab, 0x0630, 0x17b9,
    0xef4e, 0xfec7, 0xcc5c, 0xddd5, 0xa96a, 0xb8e3, 0x8a78, 0x9bf1,
    0x7387, 0x620e, 0x5095, 0x411c, 0x35a3, 0x242a, 0x16b1, 0x0738,
    0xffcf, 0xee46, 0xdcdd, 0xcd54, 0xb9eb, 0xa822, 0x92b9, 0x8330,
    0x8408, 0x9581, 0xa71a, 0xb693, 0xc22c, 0xd3a5, 0xe13e, 0xf0b7,
    0x0840, 0x19c9, 0x2b52, 0x3adb, 0x4e64, 0x5fed, 0x6d76, 0x7cff,
    0x9489, 0x8500, 0xb79b, 0xa612, 0xd2ad, 0xc324, 0xf1bf, 0xe036,
    0x18c1, 0x0948, 0x3bd3, 0x2a5a, 0x5ee5, 0x4f6c, 0x7df7, 0x6c7e,
    0xa50a, 0xb483, 0x8618, 0x9791, 0xe32e, 0xf2a7, 0xc03c, 0xd1b5,
    0x2942, 0x38cb, 0x0a50, 0x1bd9, 0x6f66, 0x7eef, 0x4c74, 0x5dfd,
    0xb58b, 0xa402, 0x9699, 0x8710, 0xf3af, 0xe226, 0xd0bd, 0xc134,
    0x39c3, 0x284a, 0x1ad1, 0x0b58, 0x7fe7, 0x6e6e, 0x5cf5, 0x4d7c,
    0xc60c, 0xd785, 0xe51e, 0xf497, 0x8028, 0x91a1, 0xa33a, 0xb2b3,
    0x4a44, 0x5bcd, 0x6956, 0x78df, 0x0c60, 0x1de9, 0x2f72, 0x3efb,
    0xd68d, 0xc704, 0xf59f, 0xe416, 0x90a9, 0x8120, 0xb3bb, 0xa232,
    0x5ac5, 0x4b4c, 0x79d7, 0x685e, 0x1ce1, 0x0d68, 0x3ff3, 0x2e7a,
    0xe70e, 0xf687, 0xc41c, 0xd595, 0xa12a, 0xb0a3, 0x8238, 0x93b1,
    0x6b46, 0x7acf, 0x4854, 0x59dd, 0x2d62, 0x3ceb, 0x0e70, 0x1ff9,
    0xf78f, 0xe606, 0xd49d, 0xc514, 0xb1ab, 0xa022, 0x92b9, 0x8330,
    0x7bc7, 0x6a4e, 0x58d5, 0x495c, 0x3de3, 0x2c6a, 0x1ef1, 0x0f78,
], dtype=np.uint16)

CRC_INIT = 0xFFFF
_GOOD_RESIDUE = 0xF0B8


def crc_ccitt(data: bytes | bytearray, init: int = CRC_INIT) -> int:
    crc = init
    for b in data:
        crc = (int(_CRC_TABLE[(crc ^ b) & 0xFF]) ^ (crc >> 8)) & 0xFFFF
    return (~crc) & 0xFFFF


def crc_check(data: bytes | bytearray, init: int = CRC_INIT) -> bool:
    """True if ``data`` (payload + 2-byte CRC) verifies."""
    crc = init
    for b in data:
        crc = (int(_CRC_TABLE[(crc ^ b) & 0xFF]) ^ (crc >> 8)) & 0xFFFF
    return crc == _GOOD_RESIDUE


# --------------------------------------------------------------------------- control signals (12-bit, 100 bd)

CS1 = 0x4D5
CS2 = 0xAB2
CS3 = 0x34B
CS4 = 0xD2C

HEADER1 = 0x55
HEADER2 = 0xAA
IDLE = 0x1E

# --------------------------------------------------------------------------- status byte

STAT_COUNTER_MASK = 0x03
STAT_HUFFMAN = 0x04
STAT_BREAKIN = 0x40
STAT_QRT = 0x80


def make_status(counter: int, huffman: bool = False, breakin: bool = False, qrt: bool = False) -> int:
    s = counter & STAT_COUNTER_MASK
    if huffman:
        s |= STAT_HUFFMAN
    if breakin:
        s |= STAT_BREAKIN
    if qrt:
        s |= STAT_QRT
    return s


# --------------------------------------------------------------------------- Huffman table (hfterm pactor.c)

_HUFFMAN = [
    (15, 0x31cf), (15, 0x51cf), (15, 0x11cf), (15, 0x61cf),
    (15, 0x21cf), (15, 0x41cf), (15, 0x01cf), (15, 0x7f0f),
    (15, 0x3f0f), (15, 0x5f0f), ( 6, 0x002c), (15, 0x1f0f),
    (15, 0x6f0f), ( 6, 0x000c), (15, 0x2f0f), (15, 0x4f0f),
    (15, 0x770f), (15, 0x370f), (15, 0x570f), (15, 0x170f),
    (15, 0x670f), (15, 0x270f), (15, 0x470f), (15, 0x070f),
    (15, 0x7b0f), (15, 0x3b0f), (15, 0x71cf), (15, 0x5b0f),
    (10, 0x0263), (15, 0x1b0f), (15, 0x0f0f), (15, 0x7f63),
    ( 2, 0x0001), (11, 0x05cf), (12, 0x0363), (13, 0x1b14),  # SPC ! " #
    (13, 0x13a8), (12, 0x0b63), (12, 0x09cf), (12, 0x0763),  # $ % & '
    ( 9, 0x01b3), ( 9, 0x0073), (12, 0x0314), (12, 0x07cf),  # ( ) * +
    ( 7, 0x0053), (11, 0x07a8), ( 7, 0x0013), (11, 0x03cf),  # , - . /
    ( 8, 0x00e3), ( 9, 0x0014), (10, 0x0168), (10, 0x0368),  # 0 1 2 3
    (10, 0x00e8), (10, 0x02a8), (10, 0x02e8), (10, 0x01e8),  # 4 5 6 7
    (10, 0x03e8), (10, 0x0128), (11, 0x0714), (14, 0x0b0f),  # 8 9 : ;
    (13, 0x0ba8), (10, 0x010f), (12, 0x0fcf), (10, 0x02b3),  # < = > ?
    (13, 0x03a8), ( 8, 0x0094), ( 8, 0x00f3), ( 8, 0x008f),  # @ A B C
    ( 7, 0x0058), ( 8, 0x0003), ( 8, 0x0033), (11, 0x0728),  # D E F G
    (10, 0x0114), ( 8, 0x004f), (10, 0x0183), (10, 0x00b3),  # H I J K
    ( 9, 0x0173), ( 9, 0x00af), ( 9, 0x000f), ( 9, 0x0028),  # L M N O
    ( 9, 0x0068), (10, 0x03af), ( 9, 0x0083), ( 7, 0x006f),  # P Q R S
    ( 8, 0x002f), (10, 0x0383), (10, 0x0063), (10, 0x00a8),  # T U V W
    (13, 0x1ba8), (14, 0x2b14), ( 9, 0x00cf), (15, 0x0b14),  # X Y Z [
    (14, 0x2b0f), (15, 0x4b14), (15, 0x3f63), (12, 0x030f),  # \ ] ^ _
    (15, 0x5f63), ( 5, 0x0002), ( 7, 0x0030), ( 6, 0x0032),  # ` a b c
    ( 5, 0x001c), ( 3, 0x0006), ( 7, 0x0070), ( 6, 0x0038),  # d e f g
    ( 6, 0x0008), ( 4, 0x000b), (11, 0x0328), ( 7, 0x0054),  # h i j k
    ( 6, 0x0010), ( 6, 0x0034), ( 4, 0x000a), ( 6, 0x0012),  # l m n o
    ( 8, 0x0043), (10, 0x01af), ( 4, 0x0007), ( 5, 0x0004),  # p q r s
    ( 5, 0x0000), ( 5, 0x001f), ( 8, 0x00c3), ( 7, 0x0018),  # t u v w
    (10, 0x0163), (10, 0x01a8), ( 7, 0x0023), (15, 0x1f63),  # x y z {
    (15, 0x6f63), (15, 0x2f63), (15, 0x4f63), (15, 0x0f63),  # | } ~ DEL
]


def huffman_encode(data: bytes) -> tuple[bytes, int]:
    """Encode ``data`` with the PACTOR Huffman table. Returns (packed bytes, total bits).

    Matches the hfterm pactor.c encoder: codes are packed LSB-first into a byte stream.
    """
    out = bytearray(len(data) * 2 + 4)
    total_bits = 0
    for b in data:
        length, hcode = _HUFFMAN[b & 0x7F]
        byte_idx = total_bits >> 3
        bit_off = total_bits & 7
        shifted = hcode << bit_off
        out[byte_idx] |= shifted & 0xFF
        if byte_idx + 1 < len(out):
            out[byte_idx + 1] |= (shifted >> 8) & 0xFF
        if byte_idx + 2 < len(out):
            out[byte_idx + 2] |= (shifted >> 16) & 0xFF
        total_bits += length
    nbytes = (total_bits + 7) >> 3
    return bytes(out[:nbytes]), total_bits


def huffman_fill_idle(buf: bytearray, start_bit: int, end_bit: int) -> None:
    """Fill bits [start_bit, end_bit) with Huffman IDLE codes.

    Remaining bits after the last complete IDLE are filled with the start of
    another IDLE code (truncated).  Since IDLE is the longest code (15 bits),
    the decoder sees an incomplete code and stops cleanly.
    """
    idle_len, idle_code = _HUFFMAN[IDLE]
    pos = start_bit
    while pos + idle_len <= end_bit:
        byte_idx = pos >> 3
        bit_off = pos & 7
        shifted = idle_code << bit_off
        buf[byte_idx] |= shifted & 0xFF
        if byte_idx + 1 < len(buf):
            buf[byte_idx + 1] |= (shifted >> 8) & 0xFF
        if byte_idx + 2 < len(buf):
            buf[byte_idx + 2] |= (shifted >> 16) & 0xFF
        pos += idle_len
    remain = end_bit - pos
    if remain > 0:
        byte_idx = pos >> 3
        bit_off = pos & 7
        mask = (1 << remain) - 1
        partial = (idle_code & mask) << bit_off
        buf[byte_idx] |= partial & 0xFF
        if byte_idx + 1 < len(buf):
            buf[byte_idx + 1] |= (partial >> 8) & 0xFF


def huffman_decode(packed: bytes, nbits: int) -> bytes:
    """Decode Huffman-coded bits. Stops at ``nbits`` or when no code matches."""
    out = bytearray()
    bitpos = 0
    masks = [0] + [(1 << i) - 1 for i in range(1, 17)]
    while bitpos < nbits:
        byte_idx = bitpos >> 3
        if byte_idx + 2 >= len(packed):
            pad = packed + b"\x00" * 3
        else:
            pad = packed
        bitbuf = pad[byte_idx] | (pad[byte_idx + 1] << 8) | (pad[byte_idx + 2] << 16)
        bitbuf >>= bitpos & 7
        found = False
        for i, (length, code) in enumerate(_HUFFMAN):
            if bitpos + length > nbits:
                continue
            if (bitbuf & masks[length]) == code:
                if i != IDLE:
                    out.append(i)
                bitpos += length
                found = True
                break
        if not found:
            break
    return bytes(out)


# --------------------------------------------------------------------------- callsign encoding

CALL_PAD = 0x0F


def encode_call(call: str) -> bytes:
    """Pad a callsign to 8 bytes with 0x0F."""
    raw = call.upper().encode("ascii")[:8]
    return raw + bytes([CALL_PAD] * (8 - len(raw)))


def decode_call(raw: bytes) -> str:
    return bytes(b for b in raw if b != CALL_PAD).decode("ascii", "replace").strip()


# --------------------------------------------------------------------------- packet build/parse

def build_packet(data: bytes, counter: int, huffman: bool = False, breakin: bool = False, qrt: bool = False,
                 crc_init: int = CRC_INIT) -> bytes:
    """Build a PACTOR-I packet (without header): data + status + CRC (LSB first)."""
    stat = make_status(counter, huffman, breakin, qrt)
    payload = data + bytes([stat])
    crc = crc_ccitt(payload, crc_init)
    return payload + bytes([crc & 0xFF, crc >> 8])


def parse_packet(raw: bytes, crc_init: int = CRC_INIT) -> dict | None:
    """Parse a PACTOR-I packet (without header). Returns dict or None if CRC fails."""
    if len(raw) < 3:
        return None
    if not crc_check(raw, crc_init):
        return None
    stat = raw[-3]
    return {
        "data": raw[:-3],
        "counter": stat & STAT_COUNTER_MASK,
        "huffman": bool(stat & STAT_HUFFMAN),
        "breakin": bool(stat & STAT_BREAKIN),
        "qrt": bool(stat & STAT_QRT),
    }


# --------------------------------------------------------------------------- CS bits

def cs_bits(cs: int) -> list[int]:
    """12 bits of a control signal, LSB first."""
    return [(cs >> i) & 1 for i in range(12)]


def cs_from_bits(bits) -> int | None:
    val = sum(int(b) << i for i, b in enumerate(bits[:12]))
    for cs in (CS1, CS2, CS3, CS4):
        if val == cs:
            return cs
    return None
