"""Passband QAM for 16 kHz linear VoIP legs: 38.4 kbit/s (64-QAM) or 51.2 kbit/s (256-QAM).

A VoIP-native waveform (no radio could carry it), with the same AX.25/KISS stack on top::

    6400 symbols/s, carrier 4 kHz, root-raised-cosine beta 0.2 -> occupies 0.16-7.84 kHz of the 0-8 kHz leg

Each burst: a known 128-symbol QPSK preamble, then HDLC (or FX.25) bits through the G3RUH self-synchronising
scrambler, mapped onto a Gray-coded square constellation (6 or 8 bits per symbol), then a few symbols of silence.
Every burst is self-contained: the receiver re-acquires from the preamble, so there is no carrier-phase
ambiguity to resolve and no state carried between transmissions.

Receiver: upsample x4 to 64 kHz -> mix down -> matched RRC (which also rejects the up-sampling images and the
2fc term) -> squelch opens on energy -> preamble located by correlation (timing, carrier phase and gain in one
complex number), refined by LMS over the known preamble symbols -> per 16-symbol block, timing from
interpolated strobes (Mueller-Mueller, decision directed: Gardner's detector is weak at beta 0.2); per
symbol, a one-tap complex LMS (gain + phase, i.e. AGC and carrier tracking together)
and nearest-point decisions -> descrambler -> HDLC / FX.25. No equaliser: a linear VoIP path adds no filtering.
"""

from __future__ import annotations

import math

import numpy as np

from ..fx25 import Fx25Receiver
from ..fx25 import burst_bits as fx25_burst_bits
from .afsk import _Fir
from .g3ruh import Scrambler, _dedupe
from .hdlc import HdlcDecoder, track_burst

RS = 6400                       # symbols/s
SPS = 10                        # internal samples per symbol
INTERNAL = RS * SPS             # 64 kHz
FC = 4000.0
BETA = 0.2
SPAN = 12                       # RRC length, symbols
PRE_LEN = 128
RATES = {"qam38400": 6, "qam51200": 8}     # bits per symbol


def rrc(beta: float, span: int, sps: int) -> np.ndarray:
    t = (np.arange(span * sps + 1) - span * sps / 2) / sps
    h = np.empty_like(t)
    for i, x in enumerate(t):
        if abs(x) < 1e-9:
            h[i] = 1 - beta + 4 * beta / math.pi
        elif abs(abs(x) - 1 / (4 * beta)) < 1e-9:
            h[i] = beta / math.sqrt(2) * ((1 + 2 / math.pi) * math.sin(math.pi / (4 * beta))
                                          + (1 - 2 / math.pi) * math.cos(math.pi / (4 * beta)))
        else:
            h[i] = (math.sin(math.pi * x * (1 - beta)) + 4 * beta * x * math.cos(math.pi * x * (1 + beta))) / (
                math.pi * x * (1 - (4 * beta * x) ** 2))
    return h / math.sqrt(np.sum(h * h))


_RRC = rrc(BETA, SPAN, SPS)


def preamble_symbols() -> np.ndarray:
    """128 QPSK symbols from two maximal-length LFSRs (x^7 + x^6 + 1), unit average energy."""
    def lfsr(seed: int) -> list[int]:
        s, out = seed, []
        for _ in range(PRE_LEN):
            b = ((s >> 6) ^ (s >> 5)) & 1
            s = ((s << 1) | b) & 0x7F
            out.append(b)
        return out
    i, q = np.array(lfsr(0x5B)) * 2 - 1, np.array(lfsr(0x27)) * 2 - 1
    return (i + 1j * q) / math.sqrt(2)


PREAMBLE = preamble_symbols()


def _gray(n: int) -> int:
    return n ^ (n >> 1)


class Constellation:
    """Square M-QAM, Gray-coded per axis, scaled to unit average energy."""

    def __init__(self, bits: int) -> None:
        self.bits, self.half = bits, bits // 2
        self.L = 1 << self.half
        self.scale = 1 / math.sqrt(2 * (self.L * self.L - 1) / 3)
        self.levels = (2 * np.arange(self.L) - (self.L - 1)) * self.scale
        self.gray_to_index = {_gray(i): i for i in range(self.L)}
        self.index_to_gray = [_gray(i) for i in range(self.L)]

    def map(self, bits: list[int]) -> np.ndarray:
        h, out = self.half, []
        for k in range(0, len(bits) - len(bits) % self.bits, self.bits):
            gi = int("".join(map(str, bits[k:k + h])), 2)
            gq = int("".join(map(str, bits[k + h:k + 2 * h])), 2)
            out.append(self.levels[self.gray_to_index[gi]] + 1j * self.levels[self.gray_to_index[gq]])
        return np.array(out)

    def decide(self, y: complex) -> tuple[complex, int, int]:
        L, s = self.L, self.scale
        i = min(L - 1, max(0, int(round((y.real / s + (L - 1)) / 2))))
        q = min(L - 1, max(0, int(round((y.imag / s + (L - 1)) / 2))))
        return complex(self.levels[i], self.levels[q]), self.index_to_gray[i], self.index_to_gray[q]


class QamModulator:
    def __init__(self, fs: int = 16000, bits: int = 8, rms: float = 0.22) -> None:
        if fs != 16000:
            raise ValueError("QAM modes run on 16 kHz linear legs (L16/16000)")
        self.fs, self.const, self.rms = fs, Constellation(bits), rms
        self.bps = RS * bits
        self.fx25 = 0
        self.il2p = None
        self._phase = 0

    def flags_for(self, seconds: float) -> int:
        return max(2, math.ceil(seconds * self.bps / 8))

    def modulate(self, frames: list[bytes], txdelay: float = 0.02, txtail: float = 0.005) -> np.ndarray:
        bits = fx25_burst_bits(frames, self.flags_for(txdelay), max(4, self.flags_for(txtail)), self.fx25)
        line = Scrambler().scramble(bits)
        line += [0] * (-len(line) % self.const.bits)
        syms = np.concatenate((PREAMBLE, self.const.map(line), np.zeros(SPAN)))
        x = np.zeros(len(syms) * SPS, complex)
        x[::SPS] = syms
        base = np.convolve(x, _RRC)
        n = np.arange(len(base))
        passband = np.real(base * np.exp(2j * np.pi * FC * n / INTERNAL)) * math.sqrt(2)
        out = passband[::INTERNAL // self.fs] * (self.rms / math.sqrt(1 / SPS))
        return np.round(np.clip(out, -1, 1) * 32767).astype(np.int16)


class QamDemodulator:
    SQUELCH_DBFS = -40.0
    BLOCK = 16
    KP, KI = 3.0, 0.005
    MU = 0.05
    KF = 0.2                            # frequency-loop gain (fraction of the observed extra rotation adopted)
    IDLE_KEEP = 4096                    # internal samples of history kept while idle (> one block + filter delay)
    SEARCH = 3200                       # preamble start offsets searched after the onset estimate (2 blocks + delays)

    def __init__(self, fs: int = 16000, bits: int = 8, energy_dcd_dbfs: float | None = -45.0) -> None:
        if fs != 16000:
            raise ValueError("QAM modes run on 16 kHz linear legs (L16/16000)")
        self.fs, self.const = fs, Constellation(bits)
        self.up = INTERNAL // fs
        self._mf = _Fir(_RRC.astype(complex) * self.up, np.complex128)
        self._n = 0                                   # internal samples mixed so far
        self.z = np.zeros(0, complex)                 # matched-filter output not yet consumed
        self.z_abs = 0                                # absolute internal index of z[0]
        self.state = "idle"
        self.onset = 0
        tmpl = np.zeros(PRE_LEN * SPS, complex)
        tmpl[::SPS] = PREAMBLE
        rc = np.convolve(_RRC, _RRC)                  # TX RRC * RX RRC = raised cosine
        self._tmpl = np.convolve(tmpl, rc)[len(rc) // 2:len(rc) // 2 + PRE_LEN * SPS]
        self._tmpl_e = float(np.vdot(self._tmpl, self._tmpl).real)
        self.energy_dcd_dbfs = energy_dcd_dbfs
        self.energy_busy = False
        self.loud = False
        self.quiet = 0                                # input samples since the signal went quiet
        self.hdlc = HdlcDecoder()
        self.fx25 = Fx25Receiver()
        self._recent: dict[bytes, int] = {}
        self._bits_seen = 0
        self.frames = 0
        self.quality = 0.0
        self.evm_db = 0.0                             # error vector magnitude of the last burst (dB)
        self._reset_burst()

    def _reset_burst(self) -> None:
        self.pos = 0.0
        self.period = float(SPS)
        self.c = 1 + 0j
        self.rot = 1 + 0j                             # per-symbol carrier rotation estimate
        self.prev = 0j
        self.d_prev = 0j
        self.desc = 0
        self._err_acc, self._err_n = 0.0, 0

    @property
    def dcd(self) -> bool:
        return self.energy_busy or self.state == "data"

    @property
    def bad_fcs(self) -> int:
        return self.hdlc.bad_fcs

    @property
    def fx25_corrected(self) -> int:
        return self.fx25.corrected

    CHUNK = 160                         # input processed in <= 10 ms pieces: burst edges need that resolution
    QUIET_END = 480                     # input samples of silence that end a burst (30 ms)

    def feed(self, pcm: np.ndarray | bytes, erased: bool = False) -> list[bytes]:
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            pcm = np.frombuffer(pcm, dtype="<i2")
        out: list[bytes] = []
        for i in range(0, len(pcm), self.CHUNK):
            out += self._feed(pcm[i:i + self.CHUNK])
        return out

    def _feed(self, pcm: np.ndarray) -> list[bytes]:
        track_burst(self)
        x = np.asarray(pcm, dtype=np.float64) / 32768.0
        level = 10 * np.log10(np.mean(x * x) * 2 + 1e-12) if len(x) else -120.0
        self.loud = level > self.SQUELCH_DBFS
        if self.energy_dcd_dbfs is not None:
            self.energy_busy = level > self.energy_dcd_dbfs
        up = np.zeros(len(x) * self.up)
        up[::self.up] = x
        n = self._n + np.arange(len(up))
        self._n += len(up)
        z = self._mf(up * np.exp(-2j * np.pi * FC * n / INTERNAL) * math.sqrt(2))
        self.z = np.concatenate((self.z, z))
        out: list[bytes] = []
        if self.state == "idle":
            if self.loud:
                # Onset: the first input sample above the squelch level (independent of the block size),
                # backed off by the filters' delay; the correlator then searches SEARCH offsets from there.
                thr = 10 ** (self.SQUELCH_DBFS / 20) / 2
                first = int(np.argmax(np.abs(x) > thr))
                block_start = self.z_abs + len(self.z) - len(z)
                self.state, self.onset = "acquire", block_start + first * self.up - 2 * SPS * SPAN
            else:
                self._trim(len(self.z) - self.IDLE_KEEP)
                return out
        if self.state == "acquire":
            if self.z_abs + len(self.z) < self.onset + len(self._tmpl) + self.SEARCH:
                return out
            if not self._acquire():
                self.state = "idle"
                return out
            self.state = "data"
        self.quiet = 0 if self.loud else self.quiet + len(x)
        bits = self._symbols()
        if self.quiet >= self.QUIET_END:              # carrier gone: burst over
            self.state = "idle"
            if self._err_n:
                self.evm_db = 10 * math.log10(self._err_acc / self._err_n + 1e-12)
            self._reset_burst()
            self._trim(len(self.z) - self.IDLE_KEEP)
        if len(bits):
            data = self._descramble(bits)
            out = self.hdlc.push_bits(data) + self.fx25.feed_bits(data)
            out = _dedupe(self, out, len(data))
        return out

    def _trim(self, keep_from: int) -> None:
        if keep_from > 0:
            self.z = self.z[keep_from:]
            self.z_abs += keep_from

    # ------------------------------------------------------------------ acquisition

    def _acquire(self) -> bool:
        lo = max(0, self.onset - self.z_abs)
        hi = min(len(self.z), lo + len(self._tmpl) + self.SEARCH)
        seg = self.z[lo:hi]
        if len(seg) <= len(self._tmpl):
            return False
        corr = np.correlate(seg, self._tmpl, "valid")          # sum(seg[k+i] * conj(tmpl[i]))
        mag = np.abs(corr)
        k = int(np.argmax(mag))
        energy = float(np.vdot(seg[k:k + len(self._tmpl)], seg[k:k + len(self._tmpl)]).real)
        if energy <= 0 or mag[k] ** 2 < 0.5 * energy * self._tmpl_e:   # silence / weak correlation: not ours
            self._trim(lo + k + SPS)
            return False
        frac = 0.0
        if 0 < k < len(mag) - 1:
            a, b, c = mag[k - 1], mag[k], mag[k + 1]
            den = a - 2 * b + c
            frac = 0.5 * (a - c) / den if den else 0.0
        start = lo + k + frac                                  # z index of preamble symbol 0's peak
        self.c = self._tmpl_e / corr[k]                        # y = c * z puts symbols on the unit grid
        zp = self._interp(start + np.arange(PRE_LEN) * SPS)
        # Carrier frequency offset from the phase slope across the known preamble (a resampled path shifts the
        # 4 kHz carrier: 2 Hz at 500 ppm, enough to rotate 256-QAM's outer points out of their cells).
        ph = np.unwrap(np.angle(self.c * zp * np.conjugate(PREAMBLE)))
        slope = float(np.polyfit(np.arange(PRE_LEN), ph, 1)[0])
        self.rot = complex(math.cos(-slope), math.sin(-slope))
        self.c *= complex(math.cos(-slope * (PRE_LEN - 1) / 2), math.sin(-slope * (PRE_LEN - 1) / 2))
        # LMS over the known preamble (gain/phase refinement), with the rotation applied as in the data loop
        for s, zk in zip(PREAMBLE, zp):
            self.c *= self.rot
            y = self.c * zk
            self.c += self.MU * (s - y) * zk.conjugate() / (abs(zk) ** 2 + 1e-12)
        self.pos = start + PRE_LEN * SPS
        self.prev = complex(self.c * self._interp(np.array([self.pos - SPS]))[0])
        self.d_prev = complex(PREAMBLE[-1])
        return True

    def _interp(self, p: np.ndarray) -> np.ndarray:
        p = np.clip(p, 0, len(self.z) - 2)
        i = p.astype(np.int64)
        return self.z[i] + (self.z[i + 1] - self.z[i]) * (p - i)

    # ------------------------------------------------------------------ data

    def _symbols(self) -> np.ndarray:
        const = self.const
        mu = self.MU
        h = const.half
        out_bits: list[int] = []
        nominal = float(SPS)
        while self.pos + self.BLOCK * self.period + 2 < len(self.z):
            k = np.arange(self.BLOCK)
            p = self.pos + k * self.period
            zk = self._interp(p).tolist()
            c = self.c
            rot = self.rot
            err_acc = 0.0
            mm = 0.0
            y_prev, d_prev = self.prev, self.d_prev
            c_start = c
            for z in zk:
                c *= rot                                           # carrier frequency offset (2nd-order loop)
                y = c * z
                d, gi, gq = const.decide(y)
                e = d - y
                err_acc += (e.real * e.real + e.imag * e.imag)
                zz = z.real * z.real + z.imag * z.imag
                if (y.real * y.real + y.imag * y.imag) > 0.01:     # silence after the burst: hold, don't divide by ~0
                    c += mu * e * z.conjugate() / zz
                # Mueller-Mueller timing error (decision directed; works for any roll-off, unlike Gardner)
                mm += (d_prev.conjugate() * y - d.conjugate() * y_prev).real
                y_prev, d_prev = y, d
                for b in range(h - 1, -1, -1):
                    out_bits.append((gi >> b) & 1)
                for b in range(h - 1, -1, -1):
                    out_bits.append((gq >> b) & 1)
            if not (math.isfinite(c.real) and math.isfinite(c.imag)):
                self.quiet = self.QUIET_END                        # lost it: end the burst, re-acquire
                break
            # Phase the LMS had to add beyond the predicted rotation: integrate it into the frequency estimate.
            if abs(c_start) > 0 and abs(c) > 0:
                drift = math.atan2((c / c_start).imag, (c / c_start).real) / self.BLOCK - math.atan2(rot.imag, rot.real)
                w = math.atan2(rot.imag, rot.real) + self.KF * drift
                self.rot = rot = complex(math.cos(w), math.sin(w))
            self.c = c
            self.prev, self.d_prev = y_prev, d_prev
            self._err_acc += err_acc
            self._err_n += self.BLOCK
            # M&M error is positive when strobing early: move later.
            err = max(-1.0, min(1.0, mm / self.BLOCK))
            self.period = max(nominal * 0.995, min(nominal * 1.005, self.period + self.KI * err))
            self.pos = float(p[-1]) + self.period + self.KP * err
            mse = err_acc / self.BLOCK
            self.quality = self.quality * 0.8 + 0.2 * (1.0 if mse < const.scale ** 2 * 0.25 else -1.0)
        keep = max(0, int(self.pos) - 2 * SPS)
        self._trim(keep)
        self.pos -= keep
        return np.array(out_bits, np.uint8)

    def _descramble(self, line: np.ndarray) -> np.ndarray:
        hist = np.array([(self.desc >> (16 - m)) & 1 for m in range(17)], np.uint8)
        ext = np.concatenate((hist, line))
        self.desc = int("".join(map(str, ext[-17:])), 2)
        return ext[17:] ^ ext[:-17] ^ ext[5:-12]
