"""ARDOP receive side (4FSK frames): leader detection, symbol timing, sync, frame type, 4FSK data, RS + CRC.

An independent receiver design (not a port of ardopcf's SoundInput.c), checked against ardopcf recordings:

* The leader sin(2pi 1475 t) - sin(2pi 1525 t) = -2 cos(2pi 1500 t) sin(2pi 25 t): a 1500 Hz carrier whose envelope
  is zero exactly at every 20 ms symbol boundary, with the sign alternating per symbol. Correlating against the
  one-symbol leader template finds it, fixes symbol timing (refined to the sample) and gives each symbol's sign.
* Sync: the first symbol whose sign does not alternate.
* Frame type: ten 50-baud 4FSK symbols; decoded by minimum distance over every known frame type (soft tone
  energies), for session 0xFF and any expected session IDs -- as ardopcf does.
* Data: per symbol, the four tone energies (50 or 100 baud); hard decision -> bytes -> Reed-Solomon -> CRC.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import codec, modulate
from .codec import SAMPLE_RATE as FS

LEADER_SYM = 240


@dataclass
class Decoded:
    frame_type: int
    name: str
    session: int
    data: bytes | None            # payload (data frames: concatenated carrier data; control: None)
    ok: bool                      # CRC (data frames) or RS (ConReq/Ping/ID) succeeded
    rs_fixed: int
    start: int                    # sample index of the frame-type symbols
    end: int
    extra: dict


def frame_samples(info: codec.FrameInfo) -> int:
    """Samples after the frame-type symbols (carriers run in parallel; PSK/QAM add one reference symbol)."""
    block = info.data_len + 3 + info.rs_len if info.data_len else 0
    if info.mod == "4FSK":
        return block * 4 * FS // info.baud
    bits = {"4PSK": 2, "8PSK": 3, "16QAM": 4}[info.mod]
    return (-(-block * 8 // bits) + 1) * FS // info.baud


def _tone_energies(x: np.ndarray, pos: int, n: int, freqs) -> np.ndarray:
    seg = x[pos:pos + n]
    t = np.arange(len(seg))
    return np.array([abs(np.dot(seg, np.exp(-2j * np.pi * f / FS * t))) ** 2 for f in freqs])


class FskReceiver:
    LEADER_THRESHOLD = 0.6        # normalised correlation with the leader template
    MIN_LEADER_SYMBOLS = 4
    MIN_TYPE_SCORE = 0.7          # real frames score ~1.0; false leaders inside PSK data score ~0.4

    def __init__(self, sessions: tuple[int, ...] = ()) -> None:
        self.sessions = (0xFF, *sessions)
        self._tpl = modulate._LEADER / np.linalg.norm(modulate._LEADER)
        self._types = sorted(codec.FRAMES)

    # ------------------------------------------------------------------ leader + sync

    def _corr(self, x: np.ndarray, pos: int) -> float:
        seg = x[pos:pos + LEADER_SYM]
        e = float(np.dot(seg, seg))
        return float(np.dot(seg, self._tpl)) / (np.sqrt(e) + 1e-9) if e > 0 else 0.0

    def find_frame(self, x: np.ndarray, start: int = 0) -> int | None:
        """Sample index where the frame-type symbols begin (just after the sync), or None."""
        pos = start
        while pos + LEADER_SYM * (self.MIN_LEADER_SYMBOLS + 2) < len(x):
            if abs(self._corr(x, pos)) < self.LEADER_THRESHOLD * 0.7:
                pos += LEADER_SYM // 8
                continue
            # Refine timing: the offset (within one symbol) maximising |corr| over a few symbols.
            best = max(range(pos - LEADER_SYM // 2, pos + LEADER_SYM // 2, 2),
                       key=lambda p: sum(abs(self._corr(x, p + k * LEADER_SYM)) for k in range(3)) if p >= 0 else -1)
            best = max(range(best - 2, best + 3),
                       key=lambda p: sum(abs(self._corr(x, p + k * LEADER_SYM)) for k in range(3)) if p >= 0 else -1)
            signs = []
            p = best
            while p + 2 * LEADER_SYM < len(x):
                c = self._corr(x, p)
                if abs(c) < self.LEADER_THRESHOLD:
                    break
                signs.append(np.sign(c))
                if len(signs) >= 2 and signs[-1] == signs[-2]:
                    if len(signs) - 1 >= self.MIN_LEADER_SYMBOLS - 1:
                        return p + LEADER_SYM          # frame type starts after the sync symbol
                    break
                p += LEADER_SYM
            pos = best + LEADER_SYM
        return None

    # ------------------------------------------------------------------ frame type

    def decode_type(self, x: np.ndarray, pos: int) -> tuple[int, int, float]:
        """(frame type, session, normalised score) by minimum distance over all types and sessions."""
        en = np.array([_tone_energies(x, pos + i * LEADER_SYM, LEADER_SYM, modulate.FSK50) for i in range(10)])
        en = en / (en.sum(axis=1, keepdims=True) + 1e-12)
        best = (-1.0, 0, 0)
        for t in self._types:
            for s in self.sessions:
                syms = codec.type_symbols(t, s)
                score = float(sum(en[i, syms[i]] for i in range(10))) / 10
                if score > best[0]:
                    best = (score, t, s)
        return best[1], best[2], best[0]

    def decode_session_free(self, x: np.ndarray, pos: int, frame_type: int) -> int:
        """Session ID read from the second type byte (for monitoring a connected session)."""
        b = 0
        for i in range(4):
            en = _tone_energies(x, pos + (5 + i) * LEADER_SYM, LEADER_SYM, modulate.FSK50)
            b = (b << 2) | int(np.argmax(en))
        return b ^ frame_type

    # ------------------------------------------------------------------ data

    def _fsk_bytes(self, x: np.ndarray, pos: int, nbytes: int, baud: int) -> tuple[bytes, int]:
        n = FS // baud
        freqs = modulate.FSK50 if baud == 50 else modulate.FSK100
        out = bytearray()
        p = pos
        for _ in range(nbytes):
            v = 0
            for _ in range(4):
                v = (v << 2) | int(np.argmax(_tone_energies(x, p, n, freqs)))
                p += n
            out.append(v)
        return bytes(out), p

    def decode(self, x: np.ndarray, start: int = 0) -> Decoded | None:
        """Decode the next 4FSK frame at or after ``start`` (None if no frame / not 4FSK)."""
        pos = self.find_frame(x, start)
        if pos is None:
            return None
        t, sess, score = self.decode_type(x, pos)
        if score < self.MIN_TYPE_SCORE:
            return self.decode(x, pos)                                 # not a frame: keep searching after it
        info = codec.FRAMES[t]
        p = pos + 10 * LEADER_SYM
        extra = {"type_score": round(score, 3)}
        if info.mod != "4FSK" or info.baud == 600:
            return Decoded(t, info.name, sess, None, False, 0, pos, p + frame_samples(info),
                           dict(extra, unsupported=info.mod))
        if t in (codec.IDFRAME, codec.PING) or t in codec.CONREQ.values():
            raw, p = self._fsk_bytes(x, p, 16, 50)
            res = codec.rs_correct(raw, 4)
            ok = res is not None
            data, fixed = (res if ok else (raw[:12], 0))
            if ok:
                extra["caller"] = codec.station_from_bytes(data[:6])
                second = codec.unpacked6(data[6:12]).strip()
                extra["target" if t != codec.IDFRAME else "grid"] = (
                    second if t == codec.IDFRAME else codec.station_from_bytes(data[6:12]))
            return Decoded(t, info.name, sess, data, ok, fixed, pos, p, extra)
        if t in codec.CONACK.values() or t == codec.PINGACK:
            raw, p = self._fsk_bytes(x, p, 3, 50)
            vals = sorted(raw)
            extra["value"] = vals[1]                                   # majority of three copies
            return Decoded(t, info.name, sess, raw, raw.count(vals[1]) >= 2, 0, pos, p, extra)
        if info.data_len == 0:                                          # ACK/NAK/BREAK/IDLE/DISC/END/...
            return Decoded(t, info.name, sess, None, True, 0, pos, p, extra)
        payload = b""
        ok, fixed_total = True, 0
        for _ in range(info.carriers):
            blen = info.data_len + 3 + info.rs_len
            raw, p = self._fsk_bytes(x, p, blen, info.baud)
            res = codec.rs_correct(raw, info.rs_len)
            if res is None or not codec.check_crc(res[0], t):
                ok = False
                continue
            block, fixed = res
            fixed_total += fixed
            payload += block[1:1 + block[0]]
        return Decoded(t, info.name, sess, payload, ok, fixed_total, pos, p, extra)

    def decode_all(self, x: np.ndarray) -> list[Decoded]:
        out, pos = [], 0
        while True:
            d = self.decode(x, pos)
            if d is None:
                return out
            out.append(d)
            pos = d.end
