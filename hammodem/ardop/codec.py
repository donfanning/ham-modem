"""ARDOP bit-level codecs: frame-type table and parity, CRC-16/CRC-8, Reed-Solomon, Packed6 call signs, session ID.

Ported from ardopcf (MIT, (c) 2014-2024 Rick Muething KN6KB, John Wiseman G8BPQ, Peter LaRue KG4JJA;
github.com/pflarue/ardop) for interoperability; the Reed-Solomon conventions follow Simon Rockliff's coder that
ardopcf uses (lib/rockliff). Every detail here was checked against ardopcf's own code or output:

* CRC-16: x^16+x^12+x^5+1, init 0xFFFF, data bits shifted *into* the register LSB with the poly constant 0x8810
  applied after the shift (ardopcf's GenCRC16); the low CRC byte is XORed with the frame type on the wire.
* CRC-8 (session ID): x^8+x^7+x^3+x^2+1 in the same register form (constant 0xC6), init 0xFF; 0xFF maps to 0.
* Reed-Solomon (Rockliff): GF(2^8)/0x11d, roots alpha^1..alpha^n, message processed last byte first with the
  shortening zeros on the low-degree side, parity emitted lowest degree first -- equivalent to our textbook coder
  on reversed data + trailing zeros with reversed parity (verified against rrs.c compiled from ardopcf's tree).
* Packed6: 8 characters of 6-bit ASCII (' '..'_') in 6 bytes; call sign padded to 7 + one SSID character.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..fx25 import ReedSolomon

SAMPLE_RATE = 12000                    # ardopcf's internal rate

# ---------------------------------------------------------------------------------------------- frame types

BREAK, IDLE, DISC, END, CONREJBUSY, CONREJBW = 0x23, 0x24, 0x29, 0x2C, 0x2D, 0x2E
IDFRAME, PINGACK, PING = 0x30, 0x3D, 0x3E
CONREQ = {"200M": 0x31, "500M": 0x32, "1000M": 0x33, "2000M": 0x34,
          "200F": 0x35, "500F": 0x36, "1000F": 0x37, "2000F": 0x38}
CONACK = {200: 0x39, 500: 0x3A, 1000: 0x3B, 2000: 0x3C}
DATANAK_MIN, DATANAK_MAX, DATAACK_MIN = 0x00, 0x1F, 0xE0


@dataclass(frozen=True)
class FrameInfo:
    code: int
    name: str
    carriers: int
    mod: str             # 4FSK / 4PSK / 8PSK / 16QAM
    baud: int
    data_len: int        # data bytes per carrier (excl. count byte and CRC)
    rs_len: int          # RS parity bytes per carrier
    qual_threshold: int

    @property
    def odd(self) -> bool:
        return bool(self.code & 1)


# (codes, carriers, mod, baud, data_len, rs_len, quality threshold, name) -- parsed from ardopcf's FrameInfo()
_TABLE = [
    ((0x23, 0x24, 0x29), 1, "4FSK", 50, 0, 0, 60, {0x23: "BREAK", 0x24: "IDLE", 0x29: "DISC"}),
    ((0x2C, 0x2D, 0x2E), 1, "4FSK", 50, 0, 0, 60, {0x2C: "END", 0x2D: "ConRejBusy", 0x2E: "ConRejBW"}),
    ((0x30, *range(0x31, 0x39), 0x3E), 1, "4FSK", 50, 12, 4, 50,
     {0x30: "IDFrame", 0x3E: "Ping", **{v: f"ConReq{k}" for k, v in CONREQ.items()}}),
    ((0x39, 0x3A, 0x3B, 0x3C, 0x3D), 1, "4FSK", 50, 3, 0, 50,
     {0x3D: "PingAck", **{v: f"ConAck{k}" for k, v in CONACK.items()}}),
    ((0x40, 0x41), 1, "4PSK", 100, 64, 32, 30, "4PSK.200.100"),
    ((0x42, 0x43), 1, "4PSK", 100, 16, 8, 30, "4PSK.200.100S"),
    ((0x44, 0x45), 1, "8PSK", 100, 108, 36, 30, "8PSK.200.100"),
    ((0x46, 0x47), 1, "16QAM", 100, 128, 64, 30, "16QAM.200.100"),
    ((0x48, 0x49), 1, "4FSK", 50, 16, 4, 30, "4FSK.200.50S"),
    ((0x4A, 0x4B), 1, "4FSK", 100, 64, 16, 30, "4FSK.500.100"),
    ((0x4C, 0x4D), 1, "4FSK", 100, 32, 8, 30, "4FSK.500.100S"),
    ((0x50, 0x51), 2, "4PSK", 100, 64, 32, 50, "4PSK.500.100"),
    ((0x52, 0x53), 2, "8PSK", 100, 108, 36, 50, "8PSK.500.100"),
    ((0x54, 0x55), 2, "16QAM", 100, 128, 64, 50, "16QAM.500.100"),
    ((0x60, 0x61), 4, "4PSK", 100, 64, 32, 50, "4PSK.1000.100"),
    ((0x62, 0x63), 4, "8PSK", 100, 108, 36, 50, "8PSK.1000.100"),
    ((0x64, 0x65), 4, "16QAM", 100, 128, 64, 50, "16QAM.1000.100"),
    ((0x70, 0x71), 8, "4PSK", 100, 64, 32, 50, "4PSK.2000.100"),
    ((0x72, 0x73), 8, "8PSK", 100, 108, 36, 50, "8PSK.2000.100"),
    ((0x74, 0x75), 8, "16QAM", 100, 128, 64, 50, "16QAM.2000.100"),
    ((0x7A, 0x7B), 1, "4FSK", 600, 600, 150, 30, "4FSK.2000.600"),
    ((0x7C, 0x7D), 1, "4FSK", 600, 200, 50, 30, "4FSK.2000.600S"),
]

FRAMES: dict[int, FrameInfo] = {}
for codes, ncar, mod, baud, dlen, rlen, q, names in _TABLE:
    for c in codes:
        if isinstance(names, dict):
            name = names[c]
        else:
            name = names + (".O" if c & 1 else ".E")
        FRAMES[c] = FrameInfo(c, name, ncar, mod, baud, dlen, rlen, q)
for c in range(DATANAK_MIN, DATANAK_MAX + 1):
    FRAMES[c] = FrameInfo(c, f"DataNAK q{c & 0x1F}", 1, "4FSK", 50, 0, 0, 40)
for c in range(DATAACK_MIN, 0x100):
    FRAMES[c] = FrameInfo(c, f"DataACK q{c & 0x1F}", 1, "4FSK", 50, 0, 0, 60)
BY_NAME = {f.name: f for f in FRAMES.values()}


def mode_codes(mode: str) -> tuple[int, int]:
    """FEC mode name (e.g. ``4PSK.500.100``) -> (even code, odd code)."""
    return BY_NAME[mode + ".E"].code, BY_NAME[mode + ".O"].code


def type_parity(frame_type: int) -> int:
    """The 4FSK parity symbol sent after each frame-type byte (both use the parity of the first byte)."""
    p = 1
    for k in range(4):
        p ^= (frame_type >> (2 * (3 - k))) & 3
    return p


def type_symbols(frame_type: int, session_id: int) -> list[int]:
    """The 10 frame-type 4FSK symbols: 4 + parity for the type byte, 4 + parity for type ^ session ID."""
    out = []
    for b in (frame_type, frame_type ^ session_id):
        out += [(b >> (2 * (3 - k))) & 3 for k in range(4)] + [type_parity(frame_type)]
    return out


# ---------------------------------------------------------------------------------------------- CRCs

def crc16(data: bytes) -> int:
    reg = 0xFFFF
    for byte in data:
        for i in range(7, -1, -1):
            bit = (byte >> i) & 1
            msb = reg & 0x8000
            reg = ((reg << 1) | bit) & 0xFFFF
            if msb:
                reg ^= 0x8810
    return reg


def append_crc(data: bytes, frame_type: int) -> bytes:
    c = crc16(data)
    return data + bytes((c >> 8, (c & 0xFF) ^ frame_type))


def check_crc(block: bytes, frame_type: int) -> bool:
    c = crc16(block[:-2])
    return block[-2] == c >> 8 and block[-1] == (c & 0xFF) ^ frame_type


def crc8(data: bytes) -> int:
    reg = 0xFF
    for byte in data:
        for i in range(7, -1, -1):
            bit = (byte >> i) & 1
            msb = reg & 0x80
            reg = ((reg << 1) | bit) & 0xFF
            if msb:
                reg ^= 0xC6
    return reg


def session_id(caller: str, target: str) -> int:
    """8-bit session ID from the two call-sign strings (as ardopcf prints them, e.g. ``N0CALL-1``)."""
    sid = crc8((caller + target).encode("ascii"))
    return 0 if sid == 0xFF else sid


# ---------------------------------------------------------------------------------------------- Reed-Solomon

_RS: dict[int, ReedSolomon] = {}


def _rs(n: int) -> ReedSolomon:
    if n not in _RS:
        _RS[n] = ReedSolomon(n, fcr=1)
    return _RS[n]


def rs_append(data: bytes, rs_len: int) -> bytes:
    """``data`` followed by ``rs_len`` Rockliff-convention parity bytes."""
    if rs_len == 0:
        return bytes(data)
    pad = 255 - rs_len - len(data)
    parity = _rs(rs_len).encode(bytes(data)[::-1] + bytes(pad))
    return bytes(data) + parity[::-1]


def rs_correct(block: bytes, rs_len: int, erasures: list[int] | None = None) -> tuple[bytes, int] | None:
    """Corrected data part of ``block`` (data + parity) and the number of bytes fixed, or None."""
    if rs_len == 0:
        return bytes(block), 0
    data, parity = bytes(block[:-rs_len]), bytes(block[-rs_len:])
    pad = 255 - len(block)
    word = data[::-1] + bytes(pad) + parity[::-1]
    era = None
    if erasures:
        n = len(data)
        era = [n - 1 - p if p < n else len(word) - 1 - (p - n) for p in erasures]
    res = _rs(rs_len).decode(word, era)
    if res is None:
        return None
    fixed, count = res
    return fixed[:len(data)][::-1], count


# ---------------------------------------------------------------------------------------------- call signs

def _pack4(s: str) -> bytes:
    v = 0
    for ch in s:
        b = ord(ch)
        if ord(" ") <= b <= ord("_"):
            b -= 0x20
        elif ord("a") <= b <= ord("z"):
            b = b - 0x20 - 0x20
        else:
            raise ValueError(f"character {ch!r} cannot be Packed6-encoded")
        v = (v << 6) | (b & 0x3F)
    return v.to_bytes(3, "big")


def _unpack3(b: bytes) -> str:
    v = int.from_bytes(b, "big")
    return "".join(chr(((v >> (6 * (3 - i))) & 0x3F) + 0x20) for i in range(4))


def packed6(text: str) -> bytes:
    w = f"{text:<8.8}"
    return _pack4(w[:4]) + _pack4(w[4:])


def unpacked6(b: bytes) -> str:
    return _unpack3(b[:3]) + _unpack3(b[3:6])


def _ssid_char(ssid: str) -> str:
    n = int(ssid or "0", 36)
    if 0 <= n <= 9:
        return chr(ord("0") + n)
    if 10 <= n <= 35:
        return chr(ord("A") + n - 10)
    if 36 <= n <= 41:                        # "10".."15" read in base 36
        return chr(ord(":") + n - 36)
    raise ValueError(f"invalid SSID {ssid!r}")


def station_bytes(station: str) -> bytes:
    """``CALL`` or ``CALL-SSID`` -> 6 wire bytes (call padded to 7 characters, then the SSID character)."""
    call, _, ssid = station.upper().partition("-")
    if not 3 <= len(call) <= 7 or " " in call:
        raise ValueError(f"invalid call sign {station!r}")
    return packed6(f"{call:<7}{_ssid_char(ssid)}")


def station_from_bytes(b: bytes) -> str:
    w = unpacked6(b)
    call, sc = w[:7].rstrip(), w[7]
    if "0" <= sc <= "?":
        ssid = str(ord(sc) - ord("0"))
    elif "A" <= sc <= "Z":
        ssid = sc
    else:
        raise ValueError(f"invalid SSID character {sc!r}")
    return call if ssid == "0" else f"{call}-{ssid}"
