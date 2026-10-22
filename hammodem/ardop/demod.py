"""ARDOP receive side: leader detection, symbol timing, sync, frame type, 4FSK and PSK/QAM data, RS + CRC.

An independent receiver design (not a port of ardopcf's SoundInput.c), checked against ardopcf recordings:

* The leader sin(2pi 1475 t) - sin(2pi 1525 t) = -2 cos(2pi 1500 t) sin(2pi 25 t): a 1500 Hz carrier whose envelope
  is zero exactly at every 20 ms symbol boundary, with the sign alternating per symbol. Correlating against the
  one-symbol leader template finds it, fixes symbol timing (refined to the sample) and gives each symbol's sign.
* Sync: the first symbol whose sign does not alternate.
* Frame type: ten 50-baud 4FSK symbols; decoded by minimum distance over every known frame type (soft tone
  energies), for session 0xFF and any expected session IDs -- as ardopcf does.
* 4FSK data: per symbol, the four tone energies (50 or 100 baud); hard decision -> bytes -> Reed-Solomon -> CRC.
* PSK/QAM data (100 baud, 1-8 carriers): per carrier, each 120-sample symbol correlated with the half-sine-windowed
  carrier; the phase change from the previous symbol (starting from the reference symbol) gives the symbol, and for
  16QAM the magnitude relative to the reference symbol gives bit 3.
* 600 baud "FM only" 4FSK: as 4FSK data, 20-sample symbols on 600/1200/1800/2400 Hz (4FSK.2000.600: three RS blocks).
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
    if info.mod == "4FSK":
        nblocks, dlen, rlen = modulate.fsk_blocks(info)
        per_car = (dlen + 3 + rlen) * (nblocks if nblocks != info.carriers else 1) if info.data_len else 0
        return per_car * 4 * FS // info.baud
    block = info.data_len + 3 + info.rs_len
    bits = {"4PSK": 2, "8PSK": 3, "16QAM": 4}[info.mod]
    return (-(-block * 8 // bits) + 1) * FS // info.baud


def _tone_energies(x: np.ndarray, pos: int, n: int, freqs) -> np.ndarray:
    seg = x[pos:pos + n]
    t = np.arange(len(seg))
    return np.array([abs(np.dot(seg, np.exp(-2j * np.pi * f / FS * t))) ** 2 for f in freqs])


class Receiver:
    LEADER_THRESHOLD = 0.6        # normalised correlation with the leader template
    MIN_LEADER_SYMBOLS = 4
    MIN_TYPE_SCORE = 0.7          # real frames score ~1.0; false leaders inside PSK data score ~0.4

    def __init__(self, sessions: tuple[int, ...] = ()) -> None:
        self.sessions = (0xFF, *sessions)
        self._tpl = modulate._LEADER / np.linalg.norm(modulate._LEADER)
        self.waiting: int | None = None
        self._types = sorted(codec.FRAMES)

    # ------------------------------------------------------------------ leader + sync

    def _corr(self, x: np.ndarray, pos: int) -> float:
        seg = x[pos:pos + LEADER_SYM]
        e = float(np.dot(seg, seg))
        return float(np.dot(seg, self._tpl)) / (np.sqrt(e) + 1e-9) if e > 0 else 0.0

    def find_frame(self, x: np.ndarray, start: int = 0) -> int | None:
        """Sample index where the frame-type symbols begin (just after the sync), or None. When ``x`` ends inside a
        leader, None with ``self.waiting`` set to where it was detected (a streaming caller keeps that much)."""
        self.waiting = None
        pos = start
        while pos + LEADER_SYM * (self.MIN_LEADER_SYMBOLS + 2) < len(x):
            if abs(self._corr(x, pos)) < self.LEADER_THRESHOLD * 0.7:
                pos += LEADER_SYM // 8
                continue
            if pos + LEADER_SYM // 2 + 3 * LEADER_SYM + 2 > len(x):
                self.waiting = pos              # streaming: wait for the whole refinement window
                return None
            # Refine timing: the offset (within one symbol) maximising |corr| over a few symbols.
            best = max(range(pos - LEADER_SYM // 2, pos + LEADER_SYM // 2, 2),
                       key=lambda p: sum(abs(self._corr(x, p + k * LEADER_SYM)) for k in range(3)) if p >= 0 else -1)
            best = max(range(best - 2, best + 3),
                       key=lambda p: sum(abs(self._corr(x, p + k * LEADER_SYM)) for k in range(3)) if p >= 0 else -1)
            signs = []
            p = best
            while True:
                if p + 2 * LEADER_SYM >= len(x):
                    self.waiting = pos          # buffer ends inside the leader
                    return None
                c = self._corr(x, p)
                if abs(c) < self.LEADER_THRESHOLD:
                    break
                signs.append(np.sign(c))
                if len(signs) >= 2 and signs[-1] == signs[-2]:
                    if len(signs) - 1 >= self.MIN_LEADER_SYMBOLS - 1:
                        return self._fine_timing(x, p + LEADER_SYM)   # frame type starts after the sync symbol
                    break
                p += LEADER_SYM
            pos = best + LEADER_SYM
        return None

    def _fine_timing(self, x: np.ndarray, start: int) -> int:
        """|correlation| with the leader has peaks every 4 samples (half a 1500 Hz cycle), enough for 50/100 baud but
        not for 600 baud (20-sample symbols). The last two leader symbols (the sync pair) are always positive, so
        the signed correlation over them picks the right carrier phase."""
        def score(q):
            return self._corr(x, q - LEADER_SYM) + self._corr(x, q - 2 * LEADER_SYM) if q >= 2 * LEADER_SYM else -9
        return max(range(start - 5, start + 6), key=score)

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
        freqs = {50: modulate.FSK50, 100: modulate.FSK100, 600: modulate.FSK600}[baud]
        out = bytearray()
        p = pos
        for _ in range(nbytes):
            v = 0
            for _ in range(4):
                v = (v << 2) | int(np.argmax(_tone_energies(x, p, n, freqs)))
                p += n
            out.append(v)
        return bytes(out), p

    def body_samples(self, t: int) -> int:
        """Samples after the frame-type symbols for frame type ``t``."""
        info = codec.FRAMES[t]
        if t in (codec.IDFRAME, codec.PING) or t in codec.CONREQ.values():
            return 16 * 4 * LEADER_SYM
        if t in codec.CONACK.values() or t == codec.PINGACK:
            return 3 * 4 * LEADER_SYM
        return frame_samples(info) if info.data_len else 0

    def decode(self, x: np.ndarray, start: int = 0) -> Decoded | None:
        """Decode the next frame at or after ``start``: None if there is none; a Decoded with ``extra["truncated"]``
        and ``extra["need"]`` (the buffer length required) if ``x`` ends inside it."""
        while True:
            pos = self.find_frame(x, start)
            if pos is None:
                if self.waiting is None:
                    return None
                return Decoded(-1, "", 0, None, False, 0, self.waiting, len(x),
                               {"truncated": True, "need": len(x) + LEADER_SYM, "in_leader": True})
            if pos + 10 * LEADER_SYM > len(x):
                return Decoded(-1, "", 0, None, False, 0, pos, len(x),
                               {"truncated": True, "need": pos + 10 * LEADER_SYM})
            t, sess, score = self.decode_type(x, pos)
            if score >= self.MIN_TYPE_SCORE:
                break
            start = pos                                                 # not a frame: keep searching after it
        info = codec.FRAMES[t]
        p = pos + 10 * LEADER_SYM
        extra = {"type_score": round(score, 3)}
        need = p + self.body_samples(t)
        if need > len(x):
            return Decoded(t, info.name, sess, None, False, 0, pos, len(x), dict(extra, truncated=True, need=need))
        if info.mod in modulate.BITS:
            return self._decode_psk(x, t, info, sess, pos, p, extra)
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
        nblocks, dlen, rlen = modulate.fsk_blocks(info)
        for _ in range(nblocks):
            raw, p = self._fsk_bytes(x, p, dlen + 3 + rlen, info.baud)
            res = codec.rs_correct(raw, rlen)
            if res is None or not codec.check_crc(res[0], t):
                ok = False
                continue
            block, fixed = res
            fixed_total += fixed
            payload += block[1:1 + block[0]]
        return Decoded(t, info.name, sess, payload, ok, fixed_total, pos, p, extra)

    def _decode_psk(self, x, t, info, sess, pos, p, extra) -> Decoded:
        """Differential PSK/QAM: per carrier and symbol, correlate the half-sine-windowed carrier; the phase change
        from the previous symbol gives the low 3 bits; for 16QAM the magnitude (vs. the reference symbol's) the 4th."""
        blen = info.data_len + 3 + info.rs_len
        bits_per = modulate.BITS[info.mod]
        nsym = -(-blen * 8 // bits_per)
        k = np.arange(120)
        env = modulate._PSK_ENV
        end = p + (nsym + 1) * 120
        seg = x[p:end].reshape(nsym + 1, 120)
        payload, ok, fixed_total, qual = b"", True, 0, []
        for f in modulate.CARRIERS[info.carriers]:
            z = (seg * env) @ np.exp(-2j * np.pi * f * k / FS)
            d = z[1:] * np.conj(z[:-1])
            idx = np.round(np.angle(d) / (np.pi / 4)).astype(np.int64) % 8
            resid = np.angle(d * np.exp(-1j * idx * np.pi / 4))
            qual.append(float(np.mean(np.abs(resid))))
            if info.mod == "4PSK":
                syms = (idx // 2) & 3
            else:
                syms = idx & 7
            if info.mod == "16QAM":
                full = abs(z[0])                                       # reference symbol: full amplitude
                half = np.abs(z[1:]) < 0.75 * full
                syms = syms | (half.astype(np.int64) << 3)
            value = 0
            for sym in syms.tolist():
                value = (value << bits_per) | sym
            raw = (value >> (nsym * bits_per - blen * 8)).to_bytes(blen, "big")
            res = codec.rs_correct(raw, info.rs_len)
            if res is None or not codec.check_crc(res[0], t):
                ok = False
                continue
            block, fixed = res
            fixed_total += fixed
            payload += block[1:1 + block[0]]
        extra["phase_error_deg"] = round(float(np.degrees(np.mean(qual))), 1)
        return Decoded(t, info.name, sess, payload, ok, fixed_total, pos, end, extra)

    def decode_all(self, x: np.ndarray) -> list[Decoded]:
        out, pos = [], 0
        while True:
            d = self.decode(x, pos)
            if d is None:
                return out
            out.append(d)
            pos = d.end
