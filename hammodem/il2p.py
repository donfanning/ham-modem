"""IL2P: Improved Layer 2 Protocol (Nino Carrillo KK4HEJ), as implemented by Direwolf 1.7 and NinoTNC.

Unlike FX.25, IL2P replaces HDLC on the air: no flags, no bit stuffing, no NRZI and (at 9600) no G3RUH
scrambler -- raw bits drive the line level, MSB first:

    0x55 preamble | sync 0xF15E48 | header 13 + 2 RS parity | payload blocks, each + 2..16 RS parity | 0x55

* Header "type 1" squeezes a two-address AX.25 header into 13 bytes: callsigns as 6-bit characters in bits 0-5
  of bytes 0-11, SSIDs in byte 12, and UI / PID / control / FEC level / header type / 10-bit payload length
  running *down* bit columns 6 and 7 of bytes 0-11. Anything else (digipeaters, unusual PIDs) uses "type 0":
  the whole AX.25 frame (no FCS) is the payload.
* Header and every payload block are scrambled with IL2P's own LFSR (x^9 + x^4 + 1, with Direwolf's 5-bit
  offset) and then RS-coded (GF(2^8)/0x11d, first root 0, leading-zero shortening -- unlike FX.25).
* The payload is split into blocks of at most 239 (max-FEC, 16 parity each) or 247 bytes (2/4/6/8 parity by
  block size); larger blocks first.
* Receivers accept the sync word with one bit error, in either polarity.
* IL2Pc (``crc=True``): appends/checks 4 Hamming-encoded bytes of CRC-16-CCITT over the AX.25 payload
  (without FCS) immediately after the last payload block.  NinoTNC firmware uses this for all PSK modes.

Format details follow Direwolf 1.7's il2p_*.c and the IL2P Specification Draft v0.6 (KK4HEJ, March 2024)
for interoperability; this is an independent implementation.
Not implemented: modulo-128 frames in type 1 (they go as type 0).
"""

from __future__ import annotations

import numpy as np

from .ax25 import Address, Ax25Error, Frame
from .fx25 import ReedSolomon

PREAMBLE = 0x55
SYNC = 0xF15E48
HEADER_SIZE, HEADER_PARITY = 13, 2
MAX_PAYLOAD = 1023
SYNC_MIN_KNOWN_BITS = 16             # do not accept sync matches based on an almost entirely erased window

# IL2Pc CRC (IL2P Specification Draft v0.6, §"Optional Trailing CRC with Hamming Encoding")
_HAMMING_ENC = (0x00, 0x71, 0x62, 0x13, 0x54, 0x25, 0x36, 0x47,
                0x38, 0x49, 0x5a, 0x2b, 0x6c, 0x1d, 0x0e, 0x7f)
_HAMMING_DEC = (0x0, 0x0, 0x0, 0x3, 0x0, 0x5, 0xe, 0x7,
                0x0, 0x9, 0xe, 0xb, 0xe, 0xd, 0xe, 0xe,
                0x0, 0x3, 0x3, 0x3, 0x4, 0xd, 0x6, 0x3,
                0x8, 0xd, 0xa, 0x3, 0xd, 0xd, 0xe, 0xd,
                0x0, 0x5, 0x2, 0xb, 0x5, 0x5, 0x6, 0x5,
                0x8, 0xb, 0xb, 0xb, 0xc, 0x5, 0xe, 0xb,
                0x8, 0x1, 0x6, 0x3, 0x6, 0x5, 0x6, 0x6,
                0x8, 0x8, 0x8, 0xb, 0x8, 0xd, 0x6, 0xf,
                0x0, 0x9, 0x2, 0x7, 0x4, 0x7, 0x7, 0x7,
                0x9, 0x9, 0xa, 0x9, 0xc, 0x9, 0xe, 0x7,
                0x4, 0x1, 0xa, 0x3, 0x4, 0x4, 0x4, 0x7,
                0xa, 0x9, 0xa, 0xa, 0x4, 0xd, 0xa, 0xf,
                0x2, 0x1, 0x2, 0x2, 0xc, 0x5, 0x2, 0x7,
                0xc, 0x9, 0x2, 0xb, 0xc, 0xc, 0xc, 0xf,
                0x1, 0x1, 0x2, 0x1, 0x4, 0x1, 0x6, 0xf,
                0x8, 0x1, 0xa, 0xf, 0xc, 0xf, 0xf, 0xf)


def _crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def encode_crc(raw_no_fcs: bytes) -> bytes:
    """Four Hamming-encoded bytes for the IL2Pc CRC (CRC-16-CCITT over AX.25 data without FCS)."""
    crc = _crc16_ccitt(raw_no_fcs)
    return bytes(_HAMMING_ENC[(crc >> (12 - 4 * i)) & 0xF] for i in range(4))


def verify_crc(raw_no_fcs: bytes, crc_bytes: bytes) -> bool:
    """True if the 4 Hamming-encoded CRC bytes match the frame."""
    expected = _crc16_ccitt(raw_no_fcs)
    nibbles = [_HAMMING_DEC[b & 0x7F] for b in crc_bytes]
    got = (nibbles[0] << 12) | (nibbles[1] << 8) | (nibbles[2] << 4) | nibbles[3]
    return got == expected

_RS: dict[int, ReedSolomon] = {}


def _rs(nroots: int) -> ReedSolomon:
    if nroots not in _RS:
        _RS[nroots] = ReedSolomon(nroots, fcr=0)
    return _RS[nroots]


# ------------------------------------------------------------------------------------------- scrambler

def scramble_block(data: bytes) -> bytes:
    """IL2P transmit scrambler: the output lags the input by 5 bits, flushed at the end."""
    state = 0x00F
    out_bits: list[int] = []
    skipping = True
    for ib, byte in enumerate(data):
        for k in range(7, -1, -1):
            b = (byte >> k) & 1
            o = ((state >> 4) ^ state) & 1
            state = ((((b ^ state) & 1) << 9) | (state ^ ((state & 1) << 4))) >> 1
            if ib == 0 and k == 2:
                skipping = False
            if not skipping:
                out_bits.append(o)
    x = state
    for _ in range(5):
        out_bits.append(((x >> 4) ^ x) & 1)
        x = ((((0 ^ x) & 1) << 9) | (x ^ ((x & 1) << 4))) >> 1
    return np.packbits(np.array(out_bits[:8 * len(data)], np.uint8)).tobytes()


def descramble_block(data: bytes) -> bytes:
    state = 0x1F0
    out = bytearray(len(data))
    for i, byte in enumerate(data):
        v = 0
        for k in range(7, -1, -1):
            b = (byte >> k) & 1
            v = (v << 1) | ((b ^ state) & 1)
            state = ((state >> 1) | (b << 8)) ^ (b << 3)
        out[i] = v
    return bytes(out)


# ------------------------------------------------------------------------------------------- header

def _set(hdr: bytearray, bit: int, lsb_index: int, width: int, value: int) -> None:
    while width > 0 and value:
        if value & 1:
            hdr[lsb_index] |= 1 << bit
        value >>= 1
        lsb_index -= 1
        width -= 1


def _get(hdr: bytes, bit: int, lsb_index: int, width: int) -> int:
    result = 0
    i = lsb_index - (width - 1)
    for _ in range(width):
        result = (result << 1) | ((hdr[i] >> bit) & 1)
        i += 1
    return result


_PID_TO_IL2P = {0x01: 0x3, 0x06: 0x4, 0x07: 0x5, 0x08: 0x6, 0xCC: 0xB, 0xCD: 0xC, 0xCE: 0xD, 0xCF: 0xE, 0xF0: 0xF}
_IL2P_TO_PID = [0xF0, 0xF0, 0x20, 0x01, 0x06, 0x07, 0x08, 0xF0, 0xF0, 0xF0, 0xF0, 0xCC, 0xCD, 0xCE, 0xCF, 0xF0]
_S_CODES = {"RR": 0, "RNR": 1, "REJ": 2, "SREJ": 3}
_U_CODES = {"SABM": 0, "DISC": 1, "DM": 2, "UA": 3, "FRMR": 4, "UI": 5, "XID": 6, "TEST": 7}
_S_CTRL = {0: 0x01, 1: 0x05, 2: 0x09, 3: 0x0D}
_U_CTRL = {0: 0x2F, 1: 0x43, 2: 0x0F, 3: 0x63, 4: 0x87, 5: 0x03, 6: 0xAF, 7: 0xE3}


def _il2p_pid(pid: int | None) -> int | None:
    if pid is None:
        return None
    if pid & 0x30 in (0x10, 0x20):
        return 0x2
    return _PID_TO_IL2P.get(pid)


def _type1_header(f: Frame, max_fec: bool) -> tuple[bytearray, bytes] | None:
    """Header and payload for a type-1 frame, or None if the frame needs type 0."""
    if f.digis:
        return None
    hdr = bytearray(HEADER_SIZE)
    for base, addr in ((0, f.dest), (6, f.src)):
        if any(not (" " <= c <= "_") for c in addr.call):
            return None
        for i, c in enumerate(addr.call):
            hdr[base + i] = ord(c) - 0x20
    hdr[12] = (f.dest.ssid << 4) | f.src.ssid
    kind, pf, cmd = f.kind, int(f.poll), int(f.command)
    if kind in _S_CODES:
        control = (pf << 6) | (f.nr << 3) | (cmd << 2) | _S_CODES[kind]
        _set(hdr, 6, 4, 4, 0)
    elif kind in _U_CODES:
        if kind == "UI":
            pid = _il2p_pid(f.pid)
            if pid is None:
                return None
            _set(hdr, 6, 0, 1, 1)
            _set(hdr, 6, 4, 4, pid)
        else:
            _set(hdr, 6, 4, 4, 1)
        control = (pf << 6) | (cmd << 2) | (_U_CODES[kind] << 3)
    elif kind == "I":
        pid = _il2p_pid(f.pid)
        if pid is None:
            return None
        _set(hdr, 6, 4, 4, pid)
        control = (pf << 6) | (f.nr << 3) | f.ns
    else:
        return None
    _set(hdr, 6, 11, 7, control)
    _set(hdr, 7, 0, 1, int(max_fec))
    _set(hdr, 7, 1, 1, 1)
    if len(f.info) > MAX_PAYLOAD:
        return None
    _set(hdr, 7, 11, 10, len(f.info))
    return hdr, f.info


def _decode_type1(hdr: bytes, payload: bytes) -> Frame:
    def call(base: int) -> str:
        c = "".join(chr((hdr[base + i] & 0x3F) + 0x20) for i in range(6)).rstrip()
        if not c or not all(ch.isupper() or ch.isdigit() for ch in c):
            raise Ax25Error(f"bad IL2P callsign {c!r}")
        return c
    dest, src = Address(call(0), hdr[12] >> 4), Address(call(6), hdr[12] & 0xF)
    pid, ui, control = _get(hdr, 6, 4, 4), _get(hdr, 6, 0, 1), _get(hdr, 6, 11, 7)
    pf = (control >> 6) & 1
    if pid == 0:                                                     # S frame
        cmd = bool(control & 0x04)
        ctrl = _S_CTRL[control & 3] | (((control >> 3) & 7) << 5) | (0x10 if pf else 0)
        return Frame(dest, src, [], ctrl, None, b"", cmd)
    if pid == 1:                                                     # U frame other than UI
        cmd = bool(control & 0x04)
        ctrl = _U_CTRL[(control >> 3) & 7] | (0x10 if pf else 0)
        return Frame(dest, src, [], ctrl, 0xF0 if (control >> 3) & 7 == 5 else None, payload, cmd)
    if ui:
        cmd = bool(control & 0x04)
        return Frame(dest, src, [], 0x03 | (0x10 if pf else 0), _IL2P_TO_PID[pid], payload, cmd)
    ctrl = (((control >> 3) & 7) << 5) | ((control & 7) << 1) | (0x10 if pf else 0)
    return Frame(dest, src, [], ctrl, _IL2P_TO_PID[pid], payload, True)


# ------------------------------------------------------------------------------------------- payload

def payload_blocks(size: int, max_fec: bool) -> tuple[list[int], int] | None:
    """Block sizes (large blocks first) and parity symbols per block."""
    if size == 0:
        return [], 0
    if not 0 < size <= MAX_PAYLOAD:
        return None
    count = (size + (238 if max_fec else 246)) // (239 if max_fec else 247)
    small = size // count
    large_n = size - count * small
    if max_fec:
        parity = 16
    else:
        parity = 2 if small <= 61 else 4 if small <= 123 else 6 if small <= 185 else 8
    return [small + 1] * large_n + [small] * (count - large_n), parity


def encode_frame(raw: bytes, max_fec: bool = True, crc: bool = False) -> bytes:
    """Sync word + header + payload for one AX.25 frame (without FCS), optionally with IL2Pc CRC."""
    f = Frame.decode(raw)
    t1 = _type1_header(f, max_fec)
    if t1 is not None:
        hdr, payload = t1
    else:
        hdr = bytearray(HEADER_SIZE)
        _set(hdr, 7, 0, 1, int(max_fec))
        _set(hdr, 7, 11, 10, len(raw))
        payload = raw
    if len(payload) > MAX_PAYLOAD:
        raise Ax25Error("frame too long for IL2P")
    shdr = scramble_block(bytes(hdr))
    out = SYNC.to_bytes(3, "big") + shdr + _rs(HEADER_PARITY).encode(shdr)
    blocks, parity = payload_blocks(len(payload), max_fec)
    pos = 0
    for size in blocks:
        sb = scramble_block(payload[pos:pos + size])
        out += sb + _rs(parity).encode(sb)
        pos += size
    if crc:
        out += encode_crc(raw[:-2])         # CRC over AX.25 data without 2-byte FCS
    return out


def _bits_msb(data: bytes) -> list[int]:
    return [(b >> k) & 1 for b in data for k in range(7, -1, -1)]


def burst_levels(frames: list[bytes], preamble_bytes: int, postamble_bytes: int = 1, max_fec: bool = True,
                 invert: bool = False, crc: bool = False) -> list[int]:
    """Line levels (no NRZI) for one transmission of IL2P frames."""
    data = bytes((PREAMBLE,)) * max(1, preamble_bytes)
    for raw in frames:
        data += encode_frame(raw, max_fec, crc)
    data += bytes((PREAMBLE,)) * max(1, postamble_bytes)
    bits = _bits_msb(data)
    return [b ^ 1 for b in bits] if invert else bits


# ------------------------------------------------------------------------------------------- receive

class Il2pReceiver:
    """IL2P receiver fed with *raw line levels* (before NRZI and descrambling), vectorised: the sync search is a
    24-bit window match over the block (<= 1 bit error, either polarity) and header/payload bytes are sliced out
    of the bit stream once their length is known.

    ``feed_bits(bits, erased=None)`` accepts an optional per-bit erasure mask (same length as ``bits``, True =
    erased).  Erased bytes are passed to the RS decoder so errors-and-erasures decoding doubles the repair reach.
    """

    BATCH = 256

    def __init__(self, crc_mode: bool = False) -> None:
        self.pend = np.zeros(0, np.uint8)       # bits not yet consumed
        self.pend_era = np.zeros(0, bool)        # erasure flags parallel to pend
        self.state = "search"
        self.polarity = 0
        self.need = 0                           # bytes wanted in header/payload state
        self.hdr: bytes | None = None
        self.blocks: list[int] = []
        self.parity = 0
        self.max_fec = True
        self.frames = 0
        self.corrected = 0
        self.erasures = 0                        # bytes passed to RS as erased (across all frames)
        self.crc_mode = crc_mode                 # IL2Pc: expect 4 Hamming-encoded CRC bytes after payload
        self._pending_frame: bytes | None = None

    def feed_bits(self, bits, erased=None) -> list[bytes]:
        """Feed raw line levels; ``erased`` is an optional per-bit erasure mask."""
        bits = np.asarray(bits, np.uint8)
        era = np.zeros(len(bits), bool) if erased is None else np.asarray(erased, bool)
        self.pend = np.concatenate((self.pend, bits))
        self.pend_era = np.concatenate((self.pend_era, era))
        out: list[bytes] = []
        while True:
            if self.state == "search":
                if len(self.pend) < max(24, self.BATCH):
                    return out
                win = np.lib.stride_tricks.sliding_window_view(self.pend, 24)
                b = np.packbits(win, axis=1, bitorder="big").astype(np.uint32)
                val = (b[:, 0] << 16) | (b[:, 1] << 8) | b[:, 2]
                era_win = np.lib.stride_tricks.sliding_window_view(self.pend_era, 24)
                eb = np.packbits(era_win, axis=1, bitorder="big").astype(np.uint32)
                era_val = (eb[:, 0] << 16) | (eb[:, 1] << 8) | eb[:, 2]
                known = np.uint32(0xFFFFFF) ^ era_val
                known_enough = (24 - np.bitwise_count(era_val)) >= SYNC_MIN_KNOWN_BITS
                dist = np.bitwise_count((val ^ SYNC) & known)
                dist_inv = np.bitwise_count(((~val & 0xFFFFFF) ^ SYNC) & known)
                hit = np.flatnonzero(known_enough & ((dist <= 1) | (dist_inv <= 1)))
                if len(hit) == 0:
                    self.pend = self.pend[-23:]
                    self.pend_era = self.pend_era[-23:]
                    return out
                i = int(hit[0])
                self.polarity = 0 if int(dist[i]) <= 1 else 1
                self.pend = self.pend[i + 24:]
                self.pend_era = self.pend_era[i + 24:]
                self.state, self.need = "header", HEADER_SIZE + HEADER_PARITY
                continue
            if len(self.pend) < 8 * self.need:
                return out
            nbits = 8 * self.need
            chunk = np.packbits(self.pend[:nbits], bitorder="big")
            era_chunk = self.pend_era[:nbits]
            if self.polarity:
                chunk = ~chunk
            data = chunk.tobytes()
            self.pend = self.pend[nbits:]
            self.pend_era = self.pend_era[nbits:]
            if self.state == "crc":
                if self._pending_frame is not None and verify_crc(self._pending_frame[:-2], data):
                    out.append(self._pending_frame)
                self._pending_frame = None
                self.state = "search"
                continue
            if self.state == "header":
                if not self._header(data, era_chunk):
                    self.state = "search"
                elif self.need == 0:
                    frame = self._finish(b"", np.zeros(0, bool))
                    if frame is not None:
                        self._deliver(out, frame)
                continue
            frame = self._finish(data, era_chunk)
            if frame is not None:
                self._deliver(out, frame)

    def _deliver(self, out: list, frame: bytes) -> None:
        if self.crc_mode:
            self._pending_frame = frame
            self.state = "crc"
            self.need = 4
        else:
            out.append(frame)

    def _header(self, data: bytes, era_bits: np.ndarray) -> bool:
        era = self._byte_erasures(era_bits, len(data))
        res = _rs(HEADER_PARITY).decode(data, era or None)
        if res is None:
            return False
        if era:
            self.erasures += len(era)
        self.hdr = descramble_block(res[0][:HEADER_SIZE])
        self.corrected = res[1]
        self.max_fec = bool(_get(self.hdr, 7, 0, 1))
        plan = payload_blocks(_get(self.hdr, 7, 11, 10), self.max_fec)
        if plan is None:
            return False
        self.blocks, self.parity = plan
        self.need = sum(self.blocks) + self.parity * len(self.blocks)
        self.state = "payload"
        return True

    def _finish(self, data: bytes, era_bits: np.ndarray) -> bytes | None:
        self.state = "search"
        payload = b""
        pos = 0
        pos_bits = 0
        fixed = self.corrected
        for size in self.blocks:
            block_bytes = size + self.parity
            block_era = era_bits[pos_bits * 8:(pos_bits + block_bytes) * 8] if len(era_bits) else np.zeros(0, bool)
            era = self._byte_erasures(block_era, block_bytes)
            block = data[pos:pos + block_bytes]
            res = _rs(self.parity).decode(block, era or None)
            if res is None:
                return None
            if era:
                self.erasures += len(era)
            fixed += res[1]
            payload += descramble_block(res[0][:size])
            pos += block_bytes
            pos_bits += block_bytes
        try:
            if _get(self.hdr, 7, 1, 1):
                raw = _decode_type1(self.hdr, payload).encode()
            else:
                Frame.decode(payload)                          # type 0: the payload is the AX.25 frame
                raw = payload
        except (Ax25Error, KeyError, IndexError):
            return None
        self.frames += 1
        self.corrected = fixed
        return raw

    @staticmethod
    def _byte_erasures(era_bits: np.ndarray, n_bytes: int) -> list[int]:
        """Convert a per-bit erasure mask to a sorted list of erased byte positions."""
        if len(era_bits) == 0 or not era_bits.any():
            return []
        n = min(len(era_bits), n_bytes * 8)
        reshaped = np.zeros(n_bytes * 8, bool)
        reshaped[:n] = era_bits[:n]
        return list(np.flatnonzero(reshaped.reshape(n_bytes, 8).any(axis=1)))
