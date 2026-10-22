"""9600 bd G3RUH / K9NG baseband FSK.

Line coding, as Direwolf does it (needed for interop): HDLC -> NRZI -> self-synchronising scrambler
1 + x^12 + x^17 -> +/-1 levels -> raised-cosine low-pass -> straight into the FM modulator (here: the audio leg).
Polarity does not matter: an inverted signal descrambles to inverted NRZI, which decodes the same.

The signal occupies roughly 0-7 kHz, so it needs a linear 16 kHz (or better) leg end to end -- L16/slin16 and a
ConfBridge mixing at >= 16 kHz. G.711's 4 kHz Nyquist limit cannot carry it, and the demodulator refuses 8 kHz.

DSP runs at an internal 48 kHz (5 samples per symbol): transmit shapes there and decimates to the leg rate;
receive interpolates up, low-passes, removes DC, then slices and recovers the clock with the same PLL scheme as the
AFSK receiver.
"""

from __future__ import annotations

import math

import numpy as np

from .afsk import _Fir, lowpass_taps
from .hdlc import HdlcDecoder, NrziDecoder, NrziEncoder, burst_bits

BAUD = 9600
INTERNAL = 48000
SPS = INTERNAL // BAUD                  # 5


def _check_rate(fs: int) -> int:
    if fs < 16000 or INTERNAL % fs:
        raise ValueError(f"G3RUH 9600 needs a sample rate of 16000, 24000 or 48000 Hz (got {fs}); "
                         "G.711 (8 kHz) cannot carry it -- use an L16/16 kHz leg")
    return INTERNAL // fs


def raised_cosine(beta: float, span: int, sps: int) -> np.ndarray:
    t = (np.arange(span * sps + 1) - span * sps / 2) / sps
    h = np.sinc(t)
    denom = 1 - (2 * beta * t) ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        h *= np.where(np.abs(denom) < 1e-8, np.pi / 4, np.cos(np.pi * beta * t) / denom)
    return h


class Scrambler:
    """Direwolf's ``scramble``: out = in ^ s[16] ^ s[11], output shifted into the state."""

    def __init__(self) -> None:
        self.state = 0

    def scramble(self, bits: list[int]) -> list[int]:
        s, out = self.state, []
        for b in bits:
            o = (b ^ (s >> 16) ^ (s >> 11)) & 1
            s = ((s << 1) | o) & 0x1FFFF
            out.append(o)
        self.state = s
        return out


class G3ruhModulator:
    def __init__(self, fs: int = 16000, amplitude: float = 0.5, beta: float = 0.5) -> None:
        self.fs, self.amplitude = fs, amplitude
        self.decim = _check_rate(fs)
        self.nrzi, self.scr = NrziEncoder(), Scrambler()
        self._shape = _Fir(raised_cosine(beta, 8, SPS))
        self._phase = 0                    # decimation phase carried across bursts
        self.baud = BAUD

    def flags_for(self, seconds: float) -> int:
        return max(1, math.ceil(seconds * BAUD / 8))

    def modulate_bits(self, bits: list[int]) -> np.ndarray:
        levels = np.array(self.scr.scramble(self.nrzi.encode(bits)), dtype=np.float64) * 2 - 1
        x = np.zeros(len(levels) * SPS)
        x[::SPS] = levels
        y = self._shape(x)
        start = (-self._phase) % self.decim
        out = y[start::self.decim]
        self._phase = (self._phase + len(y)) % self.decim
        return np.round(np.clip(out * self.amplitude, -1, 1) * 32767).astype(np.int16)

    def modulate(self, frames: list[bytes], txdelay: float = 0.1, txtail: float = 0.01) -> np.ndarray:
        # A few trailing flags flush the shaping filter and let the far scrambler/PLL finish the last frame.
        return self.modulate_bits(burst_bits(frames, self.flags_for(txdelay), max(3, self.flags_for(txtail))))


class G3ruhDemodulator:
    INERTIA_LOCKED = 0.74
    INERTIA_SEARCH = 0.50
    DCD_THRESHOLD = 0.4

    def __init__(self, fs: int = 16000, energy_dcd_dbfs: float | None = -45.0) -> None:
        self.fs = fs
        self.interp = _check_rate(fs)
        # One filter does both jobs: interpolation image rejection and the receive low-pass. Native 48 kHz
        # input decodes best with a wide, short filter (Direwolf's gen_packets ramp: 53 -> 60 of Direwolf's 65);
        # interpolated 16 kHz input needs the narrower, longer one to keep images and in-band noise down.
        cutoff, span = (0.8, 4) if self.interp == 1 else (0.6, 8)
        self._lp = _Fir(lowpass_taps(cutoff * BAUD, INTERNAL, span * SPS * 2 + 1) * self.interp)
        self._dc_win = 64 * SPS
        self._dc_hist = np.zeros(self._dc_win)
        self.step = 2.0 * BAUD / INTERNAL
        self.phase = 0.0
        self.prev = 0
        self.quality = 0.0
        self.run = 0
        self.desc_state = 0
        self.nrzi = NrziDecoder()
        self.hdlc = HdlcDecoder()
        self.energy_dcd_dbfs = energy_dcd_dbfs
        self.energy_busy = False
        self.frames = 0

    @property
    def dcd(self) -> bool:
        return self.energy_busy or self.quality > self.DCD_THRESHOLD

    @property
    def bad_fcs(self) -> int:
        return self.hdlc.bad_fcs

    def feed(self, pcm: np.ndarray | bytes) -> list[bytes]:
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            pcm = np.frombuffer(pcm, dtype="<i2")
        x = np.asarray(pcm, dtype=np.float64) / 32768.0
        if self.energy_dcd_dbfs is not None and len(x):
            self.energy_busy = 10 * np.log10(np.mean(x * x) * 2 + 1e-12) > self.energy_dcd_dbfs
        up = np.zeros(len(x) * self.interp)
        up[::self.interp] = x
        y = self._lp(up)
        # Running-mean DC removal (radios add offset; a VoIP leg normally has none).
        buf = np.concatenate((self._dc_hist, y))
        c = np.cumsum(buf)
        mean = (c[self._dc_win:] - c[:-self._dc_win]) / self._dc_win
        self._dc_hist = buf[-self._dc_win:]
        return self._run((y - mean[-len(y):] > 0).tolist())

    def _run(self, levels: list[bool]) -> list[bytes]:
        out: list[bytes] = []
        step, prev, phase, quality, run, ds = self.step, self.prev, self.phase, self.quality, self.run, self.desc_state
        nrzi, hdlc = self.nrzi, self.hdlc
        for level in levels:
            level = 1 if level else 0
            if level != prev:
                quality = quality * 0.96 + (0.04 if abs(phase) < 0.35 else -0.04)
                phase *= self.INERTIA_LOCKED if hdlc.active else self.INERTIA_SEARCH
                prev = level
                run = 0
            phase += step
            if phase >= 1.0:
                phase -= 2.0
                run += 1
                if run > 24:                   # scrambled data never holds a level this long
                    quality = max(-1.0, quality - 0.1)
                bit = (level ^ (ds >> 16) ^ (ds >> 11)) & 1
                ds = ((ds << 1) | level) & 0x1FFFF
                frame = hdlc.push(nrzi.decode(bit))
                if frame is not None:
                    out.append(frame)
                    self.frames += 1
        self.prev, self.phase, self.quality, self.run, self.desc_state = (
            prev, phase, max(-1.0, min(1.0, quality)), run, ds)
        return out
