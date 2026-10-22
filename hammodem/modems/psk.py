"""Differential PSK packet modems, Direwolf-compatible: 2400 bit/s QPSK (V.26) and 4800 bit/s 8PSK (V.27).

On the air (as Direwolf 1.7's gen_tone.c sends it): an 1800 Hz carrier of constant amplitude whose phase jumps at
each symbol boundary --

* ``psk2400``: 1200 symbols/s, 2 bits per symbol, phase += Gray(dibit) x 90 deg (+45 deg each symbol: V.26
  alternative B, Direwolf's default);
* ``psk4800``: 1600 symbols/s, 3 bits per symbol, phase += Gray(tribit) x 45 deg (V.27 table).

Bits are HDLC with NRZI (or IL2P / FX.25 bits) taken MSB-first into each symbol. Both fit an ordinary 300-3300 Hz
voice channel, i.e. G.711 legs and real SSB/FM radios.

Receiver: upsample to an integer number of samples per symbol, then per symbol a least-squares fit of
A cos(wn + phi) over the exact window (ISI-free for these rectangular phase pulses even at 1.125 carrier cycles per
symbol), early-late timing per 16-symbol block, differential detection
(no carrier recovery needed; tolerant of a few Hz of offset), then NRZI + HDLC / FX.25 / IL2P.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..fx25 import Fx25Receiver
from ..fx25 import burst_bits as fx25_burst_bits
from ..il2p import Il2pReceiver
from ..il2p import burst_levels as il2p_burst_levels
from .afsk import _Fir, lowpass_taps
from .g3ruh import _dedupe
from .hdlc import HdlcDecoder, NrziDecoder, NrziEncoder, track_burst

CARRIER = 1800.0


@dataclass(frozen=True)
class PskProfile:
    name: str
    baud: int                    # symbols/s
    bits: int                    # per symbol
    step_deg: float              # phase step per Gray index
    offset_deg: float            # extra rotation each symbol (V.26 B)
    gray_to_phase: tuple[int, ...]


QPSK = PskProfile("psk2400", 1200, 2, 90.0, 45.0, (0, 1, 3, 2))
PSK8 = PskProfile("psk4800", 1600, 3, 45.0, 0.0, (1, 0, 2, 3, 6, 7, 5, 4))
PROFILES = {p.name: p for p in (QPSK, PSK8)}


def _sps(baud: int, fs: int) -> int:
    """Internal samples per symbol: smallest SPS with baud*SPS a multiple of fs, at least 16 (8PSK at 5 SPS left an
    eye about one sample wide: only one strobe offset in eight decoded)."""
    lcm = baud * fs // math.gcd(baud, fs)
    sps = lcm // baud
    while sps < 16:
        sps *= 2
    return sps


class PskModulator:
    def __init__(self, profile: PskProfile = QPSK, fs: int = 8000, amplitude: float = 0.5) -> None:
        self.p, self.fs, self.amplitude = profile, fs, amplitude
        self.nrzi = NrziEncoder()
        self.fx25 = 0
        self.il2p: bool | None = None
        self.phase = math.radians(45.0)            # Direwolf starts at 45 deg ("to mimic first attempt")
        self.carrier_phase = 0.0
        self._t = 0                                 # output samples so far (symbol timing)
        self._sym = 0                               # symbols so far
        self._pend: list[int] = []                  # bits waiting for a full symbol

    def flags_for(self, seconds: float) -> int:
        return max(1, math.ceil(seconds * self.p.baud * self.p.bits / 8))

    def modulate_bits(self, bits: list[int]) -> np.ndarray:
        return self.modulate_levels(self.nrzi.encode(bits))

    def modulate_levels(self, line: list[int]) -> np.ndarray:
        p = self.p
        bits = self._pend + list(line)
        n_sym = len(bits) // p.bits
        self._pend = bits[n_sym * p.bits:]
        if n_sym == 0:
            return np.zeros(0, np.int16)
        phases = np.empty(n_sym)
        ph = self.phase
        for k in range(n_sym):
            v = 0
            for b in bits[k * p.bits:(k + 1) * p.bits]:
                v = (v << 1) | b
            ph += math.radians(p.gray_to_phase[v] * p.step_deg + p.offset_deg)
            phases[k] = ph
        self.phase = ph % (2 * math.pi)
        # Samples belonging to each symbol (exact integer arithmetic: symbol k spans [k*fs/baud, (k+1)*fs/baud)).
        end = -(-(self._sym + n_sym) * self.fs // p.baud)
        n = end - self._t
        idx = ((self._t + np.arange(n)) * p.baud // self.fs) - self._sym
        idx = np.clip(idx, 0, n_sym - 1)
        w = 2 * math.pi * CARRIER / self.fs
        carrier = self.carrier_phase + w * np.arange(n)
        self.carrier_phase = float((self.carrier_phase + w * n) % (2 * math.pi))
        self._t, self._sym = end, self._sym + n_sym
        return np.round(np.sin(carrier + phases[idx]) * self.amplitude * 32767).astype(np.int16)

    def modulate(self, frames: list[bytes], txdelay: float = 0.3, txtail: float = 0.03) -> np.ndarray:
        if self.il2p is not None:
            line = il2p_burst_levels(frames, self.flags_for(txdelay), self.flags_for(txtail), self.il2p)
            line += [0] * (-len(line + self._pend) % self.p.bits)
            return self.modulate_levels(line)
        bits = fx25_burst_bits(frames, self.flags_for(txdelay), self.flags_for(txtail), self.fx25)
        bits += [0, 1, 1, 1, 1, 1, 1, 0] * 2                 # flush the last partial symbol with flags
        line = self.nrzi.encode(bits)
        line += [line[-1]] * (-len(line + self._pend) % self.p.bits)
        return self.modulate_levels(line)


class PskDemodulator:
    BLOCK = 16
    KP, KI = 1.0, 0.01
    ONSET_MIN_DBFS = -60.0              # floor below which nothing counts as a burst onset
    REACQUIRE_BLOCKS = 3                # blocks of collapsed decision quality (with signal) before re-acquiring
    ACQ_SYMBOLS = 96                    # coarse timing span (48 was noisy at the bad end of Direwolf's noise ramp)
    DCD_THRESHOLD = 0.4

    def __init__(self, profile: PskProfile = QPSK, fs: int = 8000, energy_dcd_dbfs: float | None = -45.0) -> None:
        self.p, self.fs = profile, fs
        self.sps = _sps(profile.baud, fs)
        self.internal = profile.baud * self.sps
        self.up = self.internal // fs
        self._img = _Fir(lowpass_taps(min(3600.0, 0.45 * fs), self.internal, 8 * self.up * 4 + 1) * self.up)
        # Per-symbol least-squares fit of A cos(wn + phi): at 1600 bd on an 1800 Hz carrier a symbol is only 1.125
        # carrier cycles, so cos/sin are far from orthogonal over a symbol and both the Hilbert (edge transients)
        # and mix-then-integrate (2fc residue) front ends left a ~1e-3 error floor on clean 8PSK. Projecting onto
        # cos and sin and correcting by their 2x2 Gram matrix over the exact window is ISI-free at the right
        # strobe. Running sums of x*cos, x*sin, cos^2, sin^2, cos*sin let every strobe be evaluated vectorised.
        self._w = 2 * math.pi * CARRIER / self.internal
        self._acc = np.zeros(5)                                   # running totals at the end of the buffer
        self._n = 0
        self.buf = np.zeros((5, 1))                               # cumulative sums, one column per internal sample
        self.pos = float(self.sps)
        self.period = float(self.sps)
        self.prev = 0j
        self.quality = 0.0
        self.energy_dcd_dbfs = energy_dcd_dbfs
        self.energy_busy = False
        self._loud = False
        self._floor = 1.0
        self._bad_blocks = 0
        self._acquire = True                 # acquire on whatever arrives first (input may begin mid-signal)
        self._onset = 0
        self._buf_abs = 0                    # absolute internal index of buffer column 0
        self.nrzi = NrziDecoder()
        self.hdlc = HdlcDecoder()
        self.fx25 = Fx25Receiver()
        self.il2p = Il2pReceiver()
        self._recent: dict[bytes, tuple[int, int]] = {}
        self._bits_seen = 0
        self.frames = 0
        g = profile.gray_to_phase
        self._phase_to_bits = [[(v >> (profile.bits - 1 - i)) & 1 for i in range(profile.bits)]
                               for v in sorted(range(len(g)), key=lambda v: g[v])]
        self._levels = 360.0 / profile.step_deg

    @property
    def dcd(self) -> bool:
        return self.energy_busy or self.quality > self.DCD_THRESHOLD

    @property
    def bad_fcs(self) -> int:
        return self.hdlc.bad_fcs

    @property
    def fx25_corrected(self) -> int:
        return self.fx25.corrected

    def feed(self, pcm: np.ndarray | bytes, erased: bool = False) -> list[bytes]:
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            pcm = np.frombuffer(pcm, dtype="<i2")
        track_burst(self)
        x = np.asarray(pcm, dtype=np.float64) / 32768.0
        level = 10 * np.log10(np.mean(x * x) * 2 + 1e-12) if len(x) else -120.0
        if self.energy_dcd_dbfs is not None:
            self.energy_busy = level > self.energy_dcd_dbfs
        # Burst onsets at 2.5 ms resolution, relative to a tracked noise floor (an absolute threshold never saw a
        # gap once noise filled it, so later packets were never re-acquired). Direwolf separates packets by
        # ~10 ms, less than one input block, and every packet restarts the transmitter's symbol clock.
        step = max(1, self.fs // 400)
        for k in range(0, len(x), step):
            piece = x[k:k + step]
            pw = float(np.mean(piece * piece)) + 1e-14
            self._floor = pw if pw < self._floor else self._floor * 1.003
            floor = max(self._floor, 10 ** (self.ONSET_MIN_DBFS / 10))
            if not self._loud and pw > 4 * floor:                  # +6 dB above the floor: a burst begins
                first = k + int(np.argmax(np.abs(piece) > np.sqrt(2 * floor)))
                self._acquire = True
                self._onset = self._buf_abs + self.buf.shape[1] + first * self.up
                self._loud = True
            elif self._loud and pw < 2 * floor:
                self._loud = False
        up = np.zeros(len(x) * self.up)
        up[::self.up] = x
        up = self._img(up)
        n = self._n + np.arange(len(up))
        self._n += len(up)
        c, sn = np.cos(self._w * n), np.sin(self._w * n)
        terms = np.vstack((up * c, up * sn, c * c, sn * sn, c * sn))
        cum = np.cumsum(terms, axis=1) + self._acc[:, None]
        self._acc = cum[:, -1].copy()
        self.buf = np.concatenate((self.buf, cum), axis=1)
        return self._symbols()

    def _window(self, p: np.ndarray) -> np.ndarray:
        """Complex amplitude A e^{j phi} of the carrier over the symbol window ending at (fractional) index p."""
        buf, sps = self.buf, self.sps
        def at(q):
            q = np.clip(q, 0, buf.shape[1] - 2)
            i = q.astype(np.int64)
            f = q - i
            return buf[:, i] + (buf[:, i + 1] - buf[:, i]) * f
        xc, xs, cc, ss, cs = at(p) - at(p - sps)
        det = cc * ss - cs * cs + 1e-12
        i_ = (xc * ss - xs * cs) / det                             # x = I cos + Q sin over the window
        q_ = (xs * cc - xc * cs) / det
        return i_ - 1j * q_                                       # A cos(wn + phi) = A cos(phi) cos - A sin(phi) sin

    def _coarse_timing(self) -> bool:
        """Pick the strobe phase that makes the window-fit magnitude most uniform over the first ACQ_SYMBOLS
        symbols of the burst (maximising mean power instead discriminated poorly during the 8PSK flag preamble,
        whose phase steps are mostly 45 deg). False until enough signal is buffered."""
        sps = self.sps
        start = max(self.pos, self._onset - self._buf_abs + 2 * sps)   # windows entirely inside the burst
        if self.buf.shape[1] - 2 < start + (self.ACQ_SYMBOLS + 1) * sps:
            return False
        k = np.arange(self.ACQ_SYMBOLS) * sps
        offsets = np.arange(sps, dtype=np.float64)
        # At the right strobe every window holds exactly one symbol, so |y| is the same for all of them; any
        # misalignment mixes symbols across a phase step and dents |y|. Minimise the spread of |y|.
        def spread(o: float) -> float:
            a = np.abs(self._window(start + sps + k + o))
            return float(np.std(a) / (np.mean(a) + 1e-12))
        self.pos = start + sps + offsets[int(np.argmin([spread(o) for o in offsets]))]
        self.period = float(sps)
        self.prev = complex(self._window(np.array([self.pos - sps]))[0])
        return True

    def _symbols(self) -> list[bytes]:
        sps = self.sps
        if self._acquire:
            if not self._coarse_timing():
                return []
            self._acquire = False
        n_buf = self.buf.shape[1]
        diffs: list[np.ndarray] = []
        nominal = float(sps)
        while True:
            avail = int((n_buf - 2 - self.pos) // self.period) + 1
            if avail <= 0:
                break
            m = min(self.BLOCK, avail)
            p = self.pos + np.arange(m) * self.period
            y = self._window(p)
            # Early-late gate on the window-fit magnitude: it peaks exactly at the right strobe on every phase
            # transition. Positive err = the later sample is stronger: move later.
            q = self.period / 4
            ye, yl = self._window(np.minimum(p + q, n_buf - 2.0)), self._window(p - q)
            prev = np.concatenate(([self.prev], y[:-1]))
            power = float(np.mean(np.abs(y) ** 2)) + 1e-12
            err = max(-1.0, min(1.0, float(np.mean(np.abs(ye) ** 2 - np.abs(yl) ** 2)) / power))
            if power > 1e-6:
                self.period = max(nominal * 0.99, min(nominal * 1.01, self.period + self.KI * err))
            self.pos = float(p[-1]) + self.period + (self.KP * err if power > 1e-6 else 0.0)
            diffs.append(y * np.conj(prev))                         # differential detection
            self.prev = complex(y[-1])
        keep = max(0, int(self.pos) - 2 * sps)
        self.buf = self.buf[:, keep:]
        self.pos -= keep
        self._buf_abs += keep
        if not diffs:
            return []
        d = np.concatenate(diffs)
        ang = (np.degrees(np.angle(d)) - self.p.offset_deg) % 360.0
        idx = np.round(ang / self.p.step_deg).astype(np.int64) % int(self._levels)
        resid = np.abs(((ang - idx * self.p.step_deg) + 180) % 360 - 180)
        good = float(np.mean(resid < self.p.step_deg / 4))
        dd = 0.9 ** len(d)
        self.quality = max(-1.0, min(1.0, self.quality * dd + (1 - dd) * (2 * good - 1)))
        # Decisions collapsed while a signal is present (e.g. a new transmission without a detectable gap):
        # re-run the coarse timing search on what follows.
        # Collapsed = mean residual near a random decision's 0.25 step (a "fraction within a quarter step" test
        # is ~0.5 for random decisions too, and fired on merely noisy blocks).
        collapsed = float(np.mean(resid)) / self.p.step_deg > 0.2
        self._bad_blocks = self._bad_blocks + 1 if (collapsed and self.energy_busy) else 0
        if self._bad_blocks >= self.REACQUIRE_BLOCKS:
            self._bad_blocks = 0
            self._acquire = True
            self._onset = self._buf_abs + int(self.pos)
        line = np.array([b for k in idx.tolist() for b in self._phase_to_bits[k]], np.uint8)
        lv = np.concatenate(([self.nrzi.level], line))
        data = (lv[1:] == lv[:-1]).astype(np.uint8)
        self.nrzi.level = int(lv[-1])
        out = self.hdlc.push_bits(data) + self.fx25.feed_bits(data) + self.il2p.feed_bits(line)
        return _dedupe(self, out, len(data))
