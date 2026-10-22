"""Four-level baseband PAM: a VoIP-native high-speed packet modem (57.6 kbit/s on 48 kHz legs).

Not an on-air ham waveform -- no radio's audio path could carry it -- but the same AX.25/KISS stack on top, so
legacy software just sees a fast TNC. It is G3RUH's chain with two bits per symbol:

    HDLC -> NRZI -> scrambler 1+x^12+x^17 -> bit pairs -> Gray levels {-3,-1,+1,+3} -> raised cosine -> leg

========  ==========  ======  ==========  ==================
modem     symbols/s   bits/s  leg         band (beta 0.5)
========  ==========  ======  ==========  ==================
pam19200  9600        19200   16 kHz L16  0-7.2 kHz
pam57600  28800       57600   48 kHz L16  0-21.6 kHz
========  ==========  ======  ==========  ==================

A linear VoIP leg has far more SNR than an FM radio, so four levels are cheap; what four levels do not tolerate
is gain error, timing jitter and ISI. Hence: the receive filter only rejects interpolation images (the transmit
pulse is already a zero-ISI raised cosine), symbol timing comes from a Gardner detector on interpolated samples
(zero-crossing PLLs jitter on 4-level transitions), and the outer slicing threshold tracks the signal level
(E|y| = 2A for uniform levels +/-A, +/-3A). Polarity must be preserved, which a VoIP path does.
"""

from __future__ import annotations

import math

import numpy as np

from .afsk import _Fir, lowpass_taps
from .g3ruh import ERASE_TAIL, Scrambler, _dedupe, raised_cosine
from ..fx25 import Fx25Receiver
from ..fx25 import burst_bits as fx25_burst_bits
from .hdlc import HdlcDecoder, NrziDecoder, NrziEncoder, extend_bits, span_mask

SPS = 5
SQUELCH = 0.01                               # unit amplitude below which the loops hold (digital silence)
RATES = {"pam19200": (9600, 16000), "pam57600": (28800, 48000)}
GRAY = {(0, 0): -3, (0, 1): -1, (1, 1): 1, (1, 0): 3}
BETA = 0.5


def _plan(baud: int, fs: int) -> int:
    internal = baud * SPS
    if internal % fs or (1 + BETA) * baud / 2 > 0.96 * fs / 2:
        raise ValueError(f"PAM-4 at {baud} symbols/s cannot run on a {fs} Hz leg "
                         f"(needs linear audio at {next(r for b, r in RATES.values() if b == baud)} Hz)")
    return internal // fs


class PamModulator:
    def __init__(self, fs: int = 48000, baud: int = 28800, amplitude: float = 0.18) -> None:
        self.fs, self.baud, self.amplitude = fs, baud, amplitude    # 3 x 0.18 + RC overshoot stays < 0.8 FS
        self.decim = _plan(baud, fs)
        self.nrzi, self.scr = NrziEncoder(), Scrambler()
        self.fx25 = 0                       # FX.25 check bytes per frame (16/32/64), 0 = plain AX.25
        self._shape = _Fir(raised_cosine(BETA, 10, SPS))
        self._phase = 0
        self._odd: int | None = None        # a bit left over from the previous call (bursts are bit-, not pair-aligned)

    def flags_for(self, seconds: float) -> int:
        return max(1, math.ceil(seconds * self.baud * 2 / 8))

    def modulate_bits(self, bits: list[int]) -> np.ndarray:
        line = self.scr.scramble(self.nrzi.encode(bits))
        if self._odd is not None:
            line.insert(0, self._odd)
            self._odd = None
        if len(line) % 2:
            self._odd = line.pop()
        levels = np.array([GRAY[(line[i], line[i + 1])] for i in range(0, len(line), 2)], dtype=np.float64)
        x = np.zeros(len(levels) * SPS)
        x[::SPS] = levels
        y = self._shape(x)
        start = (-self._phase) % self.decim
        out = y[start::self.decim]
        self._phase = (self._phase + len(y)) % self.decim
        return np.round(np.clip(out * self.amplitude, -1, 1) * 32767).astype(np.int16)

    def modulate(self, frames: list[bytes], txdelay: float = 0.05, txtail: float = 0.01) -> np.ndarray:
        # Trailing flags flush the shaping filter and the far end's timing loop and descrambler.
        return self.modulate_bits(fx25_burst_bits(frames, self.flags_for(txdelay), max(8, self.flags_for(txtail)),
                                                  self.fx25))


class PamDemodulator:
    DCD_THRESHOLD = 0.4

    def __init__(self, fs: int = 48000, baud: int = 28800, energy_dcd_dbfs: float | None = -45.0) -> None:
        self.fs, self.baud = fs, baud
        self.interp = _plan(baud, fs)
        internal = baud * SPS
        # Flat to the band edge (0.75 x baud), rejecting interpolation images; long enough for a sharp edge.
        self._lp = _Fir(lowpass_taps(min(0.83 * baud, 0.49 * fs), internal, 16 * SPS * 2 + 1) * self.interp)
        # DC removal must be slow: a running mean over N four-level symbols wobbles by ~sqrt(5)A/sqrt(N) with the
        # data (64 symbols: 0.28A, which closed the eye); 2048 symbols: ~0.05A. A VoIP leg carries no DC anyway.
        self._dc_win = 2048 * SPS
        self._dc_hist = np.zeros(self._dc_win)
        self._buf = np.zeros(0)              # filtered samples not yet consumed by the timing loop
        self.pos = float(SPS)                # next symbol strobe, index into _buf
        self.period = float(SPS)             # tracked symbol period (absorbs clock offset)
        self.prev = 0.0                      # previous strobe value (Gardner)
        self.level = 0.0                     # tracked E|y| = 2A
        self._since_open = 0
        self.quality = 0.0
        self.desc_state = 0
        self.nrzi = NrziDecoder()
        self.hdlc = HdlcDecoder()
        self.fx25 = Fx25Receiver()
        self._recent: dict[bytes, int] = {}
        self._bits_seen = 0
        self._abs = 0                        # internal samples received
        self._buf_abs = 0                    # absolute internal index of _buf[0]
        self._spans: list[tuple[int, int]] = []
        self._erase_delay = len(self._lp.taps) - 1
        self._since = 1 << 30
        self.energy_dcd_dbfs = energy_dcd_dbfs
        self.energy_busy = False
        self.frames = 0

    @property
    def dcd(self) -> bool:
        return self.energy_busy or self.quality > self.DCD_THRESHOLD

    @property
    def bad_fcs(self) -> int:
        return self.hdlc.bad_fcs

    def feed(self, pcm: np.ndarray | bytes, erased: bool = False) -> list[bytes]:
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            pcm = np.frombuffer(pcm, dtype="<i2")
        n_int = len(pcm) * self.interp
        if erased:
            self._spans.append((self._abs, self._abs + n_int + self._erase_delay))
        self._abs += n_int
        x = np.asarray(pcm, dtype=np.float64) / 32768.0
        if self.energy_dcd_dbfs is not None and len(x):
            self.energy_busy = 10 * np.log10(np.mean(x * x) * 2 + 1e-12) > self.energy_dcd_dbfs
        up = np.zeros(len(x) * self.interp)
        up[::self.interp] = x
        y = self._lp(up)
        buf = np.concatenate((self._dc_hist, y))
        c = np.cumsum(buf)
        mean = (c[self._dc_win:] - c[:-self._dc_win]) / self._dc_win
        self._dc_hist = buf[-self._dc_win:]
        self._buf = np.concatenate((self._buf, y - mean[-len(y):]))
        return self._symbols()

    BLOCK = 16                               # symbols per timing/level update (16: best noise margin; 8 costs CPU)
    KP, KI = 0.6, 0.01                       # loop gains per block (samples per unit of normalised Gardner error)

    def _symbols(self) -> list[bytes]:
        """Timing, level and slicing run vectorised over blocks of BLOCK symbols (VoIP clock offsets are a few
        hundred ppm at most, so per-block loop updates are plenty); only the HDLC state machine is per bit."""
        buf = self._buf
        n = len(buf)
        half = SPS / 2
        nominal = float(SPS)
        decay = 0.99 ** self.BLOCK
        levels: list[np.ndarray] = []
        amps: list[np.ndarray] = []
        strobes: list[np.ndarray] = []
        while True:
            avail = int((n - 2 - self.pos) // self.period) + 1
            if avail <= 0:
                break
            k = np.arange(min(self.BLOCK, avail))
            p = self.pos + k * self.period
            i = p.astype(np.int64)
            y = buf[i] + (buf[i + 1] - buf[i]) * (p - i)
            pm = np.maximum(p - half, 0.0)
            j = pm.astype(np.int64)
            ym = buf[j] + (buf[j + 1] - buf[j]) * (pm - j)
            prev = np.concatenate(([self.prev], y[:-1]))
            ay = np.abs(y)
            mean_ay = float(ay.mean())
            if mean_ay < 2 * SQUELCH:
                self._since_open = 0                   # silence: hold timing, restart level acquisition
                self.level = mean_ay
            else:
                # Symmetric average of |y| = 2A, fast just after the squelch opens so it settles in the preamble.
                rate = 0.5 if self._since_open < 400 else 0.1
                self.level += (mean_ay - self.level) * rate
                self._since_open += len(k)
            a = self.level * 0.5
            shift = 0.0
            if a > SQUELCH:
                err = float(np.mean(ym * (y - prev))) / (4 * a * a)     # Gardner; positive = strobing late
                err = max(-1.0, min(1.0, err))
                self.period = max(nominal * 0.99, min(nominal * 1.01, self.period - self.KI * err))
                shift = -self.KP * err
                ideal = np.where(ay < 2 * a, a, 3 * a)
                good = float(np.mean(np.abs(ay - ideal) < 0.5 * a))
                self.quality = self.quality * decay + (1 - decay) * (2 * good - 1)
            else:
                self.quality = self.quality * decay - (1 - decay)
            self.prev = float(y[-1])
            self.pos = float(p[-1]) + self.period + shift
            levels.append(y)
            amps.append(np.full(len(y), a))
            strobes.append(p + self._buf_abs)
        keep = max(0, int(self.pos) - SPS)          # history for the half-symbol sample
        self._buf = buf[keep:]
        self.pos -= keep
        self._buf_abs += keep
        self.quality = max(-1.0, min(1.0, self.quality))
        if not levels:
            return []
        y = np.concatenate(levels)
        a = np.concatenate(amps)
        line = np.empty(2 * len(y), np.uint8)       # Gray: b0 = sign, b1 = inner level
        line[0::2] = y >= 0
        line[1::2] = np.abs(y) < 2 * a
        # Descramble (out = in ^ in[-17] ^ in[-12]) and NRZI-decode, vectorised with carried history.
        hist = np.array([(self.desc_state >> (16 - m)) & 1 for m in range(17)], np.uint8)   # oldest first
        ext = np.concatenate((hist, line))
        bits = ext[17:] ^ ext[:-17] ^ ext[5:-12]
        self.desc_state = int("".join(map(str, ext[-17:])), 2)
        lv = np.concatenate(([self.nrzi.level], bits))
        data = (lv[1:] == lv[:-1]).astype(np.uint8)
        self.nrzi.level = int(lv[-1])
        out: list[bytes] = []
        push = self.hdlc.push
        for d in data.tolist():
            frame = push(d)
            if frame is not None:
                out.append(frame)
        ext = None
        if self._spans:
            at = np.concatenate(strobes).astype(np.int64)
            sym = span_mask(self._spans, int(at[0]), int(at[-1] - at[0]) + 1)[at - at[0]]
            ext, self._since = extend_bits(np.repeat(sym, 2), self._since, ERASE_TAIL)
            self._spans = [sp for sp in self._spans if sp[1] > at[-1]]
        else:
            self._since += len(data)
        out += self.fx25.feed_bits(data, ext)
        return _dedupe(self, out, len(data))

    @property
    def fx25_corrected(self) -> int:
        return self.fx25.corrected
