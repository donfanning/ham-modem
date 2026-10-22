"""FX.25: Reed-Solomon forward error correction wrapped around unchanged AX.25 frames (Stensat Group, 2006).

On the air, after the usual flag preamble::

    correlation tag (64 bits, LSB first) | RS codeword: k data bytes + 16/32/64 check bytes (LSB first)

The k data bytes are the ordinary HDLC frame -- opening flag, bit-stuffed AX.25 frame with FCS, closing flag --
packed LSB first and padded with flag patterns. So a receiver that knows nothing of FX.25 still finds and decodes
the AX.25 frame inside (the check bytes look like line noise to it); one that does can repair up to nroots/2 bad
bytes, e.g. a burst of bit errors from a lost RTP packet.

The tag selects one of eleven RS(n, k) codes over GF(2^8) (field polynomial 0x11d, first consecutive root 1,
primitive element 1), shortened from RS(255, 255 - nroots). Shortening follows Direwolf, which is what matters for
interop: the k data bytes are padded with *trailing* zeros to 255 - nroots before encoding (not the textbook
leading zeros -- the parity differs completely; found by checking Direwolf's codewords, see lab notes).
"""

from __future__ import annotations

import numpy as np

from .modems.hdlc import FLAG_BITS, HdlcDecoder, stuffed_bits
from .modems.hdlc import burst_bits as hdlc_burst_bits

# tag number: (tag value, n, k)
TAGS = {
    0x01: (0xB74DB7DF8A532F3E, 255, 239), 0x02: (0x26FF60A600CC8FDE, 144, 128), 0x03: (0xC7DC0508F3D9B09E, 80, 64),
    0x04: (0x8F056EB4369660EE, 48, 32), 0x05: (0x6E260B1AC5835FAE, 255, 223), 0x06: (0xFF94DC634F1CFF4E, 160, 128),
    0x07: (0x1EB7B9CDBC09C00E, 96, 64), 0x08: (0xDBF869BD2DBB1776, 64, 32), 0x09: (0x3ADB0C13DEAE2836, 255, 191),
    0x0A: (0xAB69DB6A543188D6, 192, 128), 0x0B: (0x4A4ABEC4A724B796, 128, 64),
}
TAG_MAX_ERRORS = 8                     # bit differences tolerated when matching a correlation tag

# ------------------------------------------------------------------------------------------- GF(2^8) and RS

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _div(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError
    return 0 if a == 0 else _EXP[(_LOG[a] - _LOG[b]) % 255]


def _poly_eval(p: list[int], x: int) -> int:
    """p[0] is the highest-degree coefficient."""
    y = 0
    for c in p:
        y = _mul(y, x) ^ c
    return y


class ReedSolomon:
    FCR = 1

    def __init__(self, nroots: int) -> None:
        self.nroots = nroots
        g = [1]
        for i in range(nroots):
            root = _EXP[self.FCR + i]
            g = [a ^ _mul(b, root) for a, b in zip(g + [0], [0] + g)]
        self.gen = g                       # highest degree first, monic

    def encode(self, data: bytes) -> bytes:
        """The nroots check bytes for ``data`` (systematic: codeword = data + check)."""
        rem = [0] * self.nroots
        g = self.gen
        for byte in data:
            fb = byte ^ rem[0]
            rem = rem[1:] + [0]
            if fb:
                lf = _LOG[fb]
                for i in range(self.nroots):
                    if g[i + 1]:
                        rem[i] ^= _EXP[lf + _LOG[g[i + 1]]]
        return bytes(rem)

    def decode(self, codeword: bytes) -> tuple[bytes, int] | None:
        """Corrected codeword and the number of byte errors fixed, or None if uncorrectable."""
        r = list(codeword)
        n = len(r)
        synd = [_poly_eval(r, _EXP[self.FCR + j]) for j in range(self.nroots)]
        if not any(synd):
            return bytes(r), 0
        # Berlekamp-Massey: error locator, lowest degree first.
        lam, b = [1], [1]
        L, m, bb = 0, 1, 1
        for k in range(self.nroots):
            d = synd[k]
            for i in range(1, L + 1):
                if i < len(lam):
                    d ^= _mul(lam[i], synd[k - i])
            if d == 0:
                m += 1
                continue
            t = lam[:]
            coef = _div(d, bb)
            shifted = [0] * m + [_mul(coef, c) for c in b]
            lam = [(lam[i] if i < len(lam) else 0) ^ (shifted[i] if i < len(shifted) else 0)
                   for i in range(max(len(lam), len(shifted)))]
            if 2 * L <= k:
                L, b, bb, m = k + 1 - L, t, d, 1
            else:
                m += 1
        while len(lam) > 1 and lam[-1] == 0:
            lam.pop()
        if len(lam) - 1 != L or L > self.nroots // 2:
            return None
        # Chien search over the (shortened) codeword: position p (0 = first byte) has locator X = a^(n-1-p).
        positions = []
        for p in range(n):
            xinv = _EXP[(255 - (n - 1 - p)) % 255]
            v = 0
            for c in reversed(lam):
                v = _mul(v, xinv) ^ c
            if v == 0:
                positions.append(p)
        if len(positions) != L:
            return None
        # Forney: omega = (S(x) * lambda(x)) mod x^nroots, both lowest degree first.
        omega = [0] * self.nroots
        for i, s in enumerate(synd):
            for j, l in enumerate(lam):
                if i + j < self.nroots:
                    omega[i + j] ^= _mul(s, l)
        dlam = [lam[i] if i % 2 == 1 else 0 for i in range(1, len(lam))]     # formal derivative
        for p in positions:
            x = _EXP[(n - 1 - p) % 255]
            xinv = _EXP[(255 - (n - 1 - p)) % 255]
            num = 0
            for c in reversed(omega):
                num = _mul(num, xinv) ^ c
            den = 0
            for c in reversed(dlam):
                den = _mul(den, xinv) ^ c
            if den == 0:
                return None
            # e = X^(1-FCR) * omega(X^-1) / lambda'(X^-1)
            mag = _div(num, den)
            if self.FCR != 1:
                mag = _mul(mag, _EXP[((1 - self.FCR) * _LOG[x]) % 255])
            r[p] ^= mag
        if any(_poly_eval(r, _EXP[self.FCR + j]) for j in range(self.nroots)):
            return None
        return bytes(r), L


_RS: dict[int, ReedSolomon] = {}


def _rs(nroots: int) -> ReedSolomon:
    if nroots not in _RS:
        _RS[nroots] = ReedSolomon(nroots)
    return _RS[nroots]


# ------------------------------------------------------------------------------------------- transmit

def _bytes_to_bits(data: bytes) -> list[int]:
    return [(b >> i) & 1 for b in data for i in range(8)]


def _bits_to_bytes(bits) -> bytes:
    return np.packbits(np.asarray(bits, np.uint8), bitorder="little").tobytes()


def choose_tag(data_len: int, nroots: int) -> int | None:
    """Smallest code with ``nroots`` check bytes whose data part holds ``data_len`` bytes."""
    fits = [(k, t) for t, (_, n, k) in TAGS.items() if n - k == nroots and k >= data_len]
    return min(fits)[1] if fits else None


def encode_bits(frame: bytes, nroots: int = 16) -> list[int] | None:
    """Correlation tag + RS codeword for one AX.25 frame (without FCS) as line bits, or None if it is too long
    for any code with that many check bytes (send it as plain AX.25 then)."""
    hdlc = list(FLAG_BITS) + stuffed_bits(frame) + list(FLAG_BITS)
    tag = choose_tag((len(hdlc) + 7) // 8, nroots)
    if tag is None:
        return None
    value, n, k = TAGS[tag]
    bits = hdlc[:]
    while len(bits) < 8 * k:                      # pad with flag patterns, bit-continuous
        bits += FLAG_BITS
    data = _bits_to_bytes(bits[:8 * k])
    nroots = n - k
    codeword = data + _rs(nroots).encode(data + bytes(255 - nroots - k))      # trailing-zero shortening
    return [(value >> i) & 1 for i in range(64)] + _bytes_to_bits(codeword)


def burst_bits(frames: list[bytes], lead_flags: int, tail_flags: int = 2, nroots: int = 16) -> list[int]:
    """Like ``hdlc.burst_bits`` but every frame that fits goes out FX.25-wrapped (``nroots`` 0: plain AX.25)."""
    if not nroots:
        return hdlc_burst_bits(frames, lead_flags, tail_flags)
    bits = list(FLAG_BITS) * lead_flags
    for frame in frames:
        fx = encode_bits(frame, nroots)
        bits += fx if fx is not None else stuffed_bits(frame) + list(FLAG_BITS)
        bits += list(FLAG_BITS)
    return bits + list(FLAG_BITS) * max(tail_flags - 1, 0)


# ------------------------------------------------------------------------------------------- receive

_TAG_ARRAY = np.array([v for v, _, _ in TAGS.values()], dtype=np.uint64)
_TAG_IDS = list(TAGS)


class Fx25Receiver:
    """Finds correlation tags in the decoded data-bit stream (vectorised), collects and repairs codewords.

    ``feed_bits(bits)`` takes the same bits an HDLC receiver gets (after NRZI) and returns repaired AX.25 frames.
    ``corrected`` counts frames that needed RS repair (and would have been lost without FX.25).
    """

    def __init__(self) -> None:
        self.hist = np.zeros(63, np.uint8)
        self.collect: tuple[int, int, int, list[np.ndarray], int] | None = None   # tag, n, k, chunks, have
        self.frames = 0
        self.corrected = 0
        self.failed = 0

    def feed_bits(self, bits) -> list[bytes]:
        bits = np.asarray(bits, np.uint8)
        out: list[bytes] = []
        pos = 0
        while pos < len(bits):
            if self.collect is not None:
                tag, n, k, chunks, have = self.collect
                take = min(8 * n - have, len(bits) - pos)
                chunks.append(bits[pos:pos + take])
                have += take
                pos += take
                if have < 8 * n:
                    self.collect = (tag, n, k, chunks, have)
                    break
                self.collect = None
                frame = self._finish(np.concatenate(chunks), n, k)
                if frame is not None:
                    out.append(frame)
                self.hist = np.zeros(63, np.uint8)       # tags never overlap a codeword just decoded
                continue
            ext = np.concatenate((self.hist, bits[pos:]))
            if len(ext) < 64:
                self.hist = ext[-63:] if len(ext) >= 63 else np.concatenate((np.zeros(63 - len(ext), np.uint8), ext))
                break
            win = np.lib.stride_tricks.sliding_window_view(ext, 64)
            regs = np.packbits(win, axis=1, bitorder="little").view("<u8")[:, 0]
            dist = np.bitwise_count(regs[:, None] ^ _TAG_ARRAY[None, :])
            hit = np.argwhere(dist <= TAG_MAX_ERRORS)
            if len(hit) == 0:
                self.hist = ext[-63:]
                break
            idx, which = hit[0]
            tag = _TAG_IDS[which]
            _, n, k = TAGS[tag]
            self.collect = (tag, n, k, [], 0)
            pos += int(idx) + 64 - len(self.hist)       # first bit after the tag, in ``bits`` coordinates
            self.hist = np.zeros(63, np.uint8)
        return out

    def _finish(self, bits: np.ndarray, n: int, k: int) -> bytes | None:
        rx = _bits_to_bytes(bits)
        nroots = n - k
        pad = 255 - nroots - k
        res = _rs(nroots).decode(rx[:k] + bytes(pad) + rx[k:])   # re-insert the shortening zeros
        if res is None:
            self.failed += 1
            return None
        codeword, fixed = res
        rx = HdlcDecoder()
        for b in _bytes_to_bits(codeword[:k]):
            frame = rx.push(b)
            if frame is not None:
                self.frames += 1
                self.corrected += bool(fixed)
                return frame
        self.failed += 1
        return None
