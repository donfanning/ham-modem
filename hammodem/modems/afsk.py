"""Audio FSK packet modem: Bell 202 (1200 bd, 1200/2200 Hz) and HF 300 bd (200 Hz shift).

Transmit is a phase-continuous tone generator driven by NRZI line levels (level 1 = mark).

Receive follows the classic sound-card design (Sailer's soundmodem, WB2OSZ's Direwolf):

    band-pass -> mix with mark and space -> windowed correlator -> |mark| , |space|
      -> one slicer per mark gain ("twist" bank) -> digital PLL bit clock -> NRZI -> HDLC

The filters run block-wise in numpy; the PLL/HDLC loop runs per sample in Python. Every slicer has its own PLL and
HDLC receiver and a frame is reported once, by whichever slicer gets it first, so audio with pre-emphasis or a
de-emphasised radio path (mark and space at different levels) still decodes.

The sample rate is a parameter: 8000 for G.711 calls, 16000 or higher for wideband legs and WAV files.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .hdlc import HdlcDecoder, NrziDecoder, NrziEncoder, burst_bits


@dataclass(frozen=True)
class AfskProfile:
    name: str
    baud: int
    mark: float
    space: float
    window_bits: float          # correlator length in bit times
    lowpass_bits: float         # post-detector low-pass cutoff as a fraction of the baud rate
    twist_db: tuple[float, ...]  # mark gain of each slicer

    @property
    def bandpass(self) -> tuple[float, float]:
        lo, hi = sorted((self.mark, self.space))
        edge = 0.5 * self.baud
        return lo - edge, hi + edge


BELL202 = AfskProfile("afsk1200", 1200, 1200.0, 2200.0, 1.2, 1.25, (-6.0, -3.0, 0.0, 3.0, 6.0))
HF300 = AfskProfile("afsk300", 300, 1600.0, 1800.0, 1.25, 0.75, (-3.0, 0.0, 3.0))
PROFILES = {p.name: p for p in (BELL202, HF300)}


def lowpass_taps(cutoff: float, fs: float, n: int) -> np.ndarray:
    """Hamming-windowed sinc, unity DC gain."""
    t = np.arange(n) - (n - 1) / 2
    h = np.sinc(2 * cutoff / fs * t) * np.hamming(n)
    return h / h.sum()


def bandpass_taps(lo: float, hi: float, fs: float, n: int) -> np.ndarray:
    t = np.arange(n) - (n - 1) / 2
    h = (2 * hi / fs * np.sinc(2 * hi / fs * t) - 2 * lo / fs * np.sinc(2 * lo / fs * t)) * np.hamming(n)
    return h


class _Fir:
    """Streaming FIR filter (real or complex input) keeping ``len(taps) - 1`` samples of history."""

    def __init__(self, taps: np.ndarray, dtype=np.float64) -> None:
        self.taps = taps
        self.hist = np.zeros(len(taps) - 1, dtype=dtype)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate((self.hist, x))
        self.hist = buf[len(buf) - len(self.hist):] if len(self.hist) else self.hist
        return np.convolve(buf, self.taps, "valid")


class AfskModulator:
    """Frames in, int16 samples out. ``txdelay`` and ``txtail`` are in seconds of flags."""

    def __init__(self, profile: AfskProfile = BELL202, fs: int = 8000, amplitude: float = 0.5) -> None:
        self.p, self.fs, self.amplitude = profile, fs, amplitude
        self.nrzi = NrziEncoder()
        self.phase = 0.0
        self.bits_sent = 0                # bit k starts at sample k * fs / baud, kept exact in integers
        self.samples_sent = 0

    def flags_for(self, seconds: float) -> int:
        return max(1, math.ceil(seconds * self.p.baud / 8))

    def modulate_bits(self, bits: list[int]) -> np.ndarray:
        """NRZI-encode raw line bits and render them, keeping phase and bit timing across calls."""
        levels = np.array(self.nrzi.encode(bits), dtype=np.int8)
        baud, fs = self.p.baud, self.fs
        end_bits = self.bits_sent + len(levels)
        n = -(-end_bits * fs // baud) - self.samples_sent
        idx = (self.samples_sent + np.arange(n, dtype=np.int64)) * baud // fs - self.bits_sent
        self.bits_sent, self.samples_sent = end_bits, self.samples_sent + n
        if n <= 0:
            return np.zeros(0, dtype=np.int16)
        w = np.where(levels[idx] == 1, self.p.mark, self.p.space) * (2 * np.pi / self.fs)
        acc = np.cumsum(w)
        phase = self.phase + acc - w
        self.phase = float((self.phase + acc[-1]) % (2 * np.pi))
        return np.round(np.sin(phase) * self.amplitude * 32767).astype(np.int16)

    def modulate(self, frames: list[bytes], txdelay: float = 0.3, txtail: float = 0.03) -> np.ndarray:
        bits = burst_bits(frames, self.flags_for(txdelay), self.flags_for(txtail))
        return self.modulate_bits(bits)


class _Slicer:
    """One decision threshold: PLL bit clock, NRZI decoder, HDLC receiver and a carrier-quality score."""

    __slots__ = ("gain", "phase", "prev", "nrzi", "hdlc", "quality")

    def __init__(self, gain: float) -> None:
        self.gain = gain
        self.phase = 0.0                  # -1..1; a bit is sampled when it wraps past +1 (mid-bit)
        self.prev = 0
        self.nrzi = NrziDecoder()
        self.hdlc = HdlcDecoder()
        self.quality = 0.0


class AfskDemodulator:
    """Samples in, good-FCS AX.25 frames out. ``dcd`` tracks whether a packet signal is present."""

    INERTIA_LOCKED = 0.74             # PLL phase kept on a transition while receiving a frame
    INERTIA_SEARCH = 0.50
    DCD_THRESHOLD = 0.4

    def __init__(self, profile: AfskProfile = BELL202, fs: int = 8000) -> None:
        self.p, self.fs = profile, fs
        spb = fs / profile.baud
        self.step = 2.0 * profile.baud / fs
        lo, hi = profile.bandpass
        nbp = int(round(spb * 2.0)) | 1
        self._bp = _Fir(bandpass_taps(max(lo, 50.0), min(hi, fs / 2 - 50.0), fs, nbp))
        nwin = max(3, int(round(spb * profile.window_bits)))
        win = np.hanning(nwin + 2)[1:-1]
        self._mark_lp = _Fir(win / win.sum(), np.complex128)
        self._space_lp = _Fir(win / win.sum(), np.complex128)
        nlp = int(round(spb * 1.5)) | 1
        self._post = _Fir(lowpass_taps(profile.baud * profile.lowpass_bits, fs, nlp), np.float64)
        self._post_s = _Fir(lowpass_taps(profile.baud * profile.lowpass_bits, fs, nlp), np.float64)
        self._wm = 2 * np.pi * profile.mark / fs
        self._ws = 2 * np.pi * profile.space / fs
        self._n = 0
        self.slicers = [_Slicer(10 ** (db / 20)) for db in profile.twist_db]
        self._recent: dict[bytes, int] = {}
        self.samples = 0
        self.frames = 0

    @property
    def dcd(self) -> bool:
        # Transition-timing quality: noise sits below 0 and packet audio, even at a few dB SNR, above 0.5.
        return any(s.quality > self.DCD_THRESHOLD for s in self.slicers)

    @property
    def bad_fcs(self) -> int:
        return max(s.hdlc.bad_fcs for s in self.slicers)

    def feed(self, pcm: np.ndarray | bytes) -> list[bytes]:
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            pcm = np.frombuffer(pcm, dtype="<i2")
        x = self._bp(np.asarray(pcm, dtype=np.float64) / 32768.0)
        n = np.arange(self._n, self._n + len(x))
        self._n += len(x)
        mark = np.abs(self._mark_lp(x * np.exp(-1j * ((self._wm * n) % (2 * np.pi)))))
        space = np.abs(self._space_lp(x * np.exp(-1j * ((self._ws * n) % (2 * np.pi)))))
        mark, space = self._post(mark), self._post_s(space)
        out: list[bytes] = []
        base = self.samples
        for s in self.slicers:
            d = (mark * s.gain - space) > 0
            self._run(s, d.tolist(), base, out)
        self.samples += len(x)
        if len(self._recent) > 64:
            horizon = self.samples - self.fs * 2
            self._recent = {k: v for k, v in self._recent.items() if v > horizon}
        return out

    def _run(self, s: _Slicer, levels: list[bool], base: int, out: list[bytes]) -> None:
        step, prev, phase, quality = self.step, s.prev, s.phase, s.quality
        nrzi, hdlc = s.nrzi, s.hdlc
        for i, level in enumerate(levels):
            level = 1 if level else 0
            if level != prev:
                # A transition should land on a bit boundary (phase near 0); score how close it was.
                quality = quality * 0.96 + (0.04 if abs(phase) < 0.35 else -0.04)
                phase *= self.INERTIA_LOCKED if hdlc.active else self.INERTIA_SEARCH
                prev = level
            phase += step
            if phase >= 1.0:
                phase -= 2.0
                frame = hdlc.push(nrzi.decode(level))
                if frame is not None:
                    when = base + i
                    seen = self._recent.get(frame)
                    if seen is None or when - seen > self.fs:
                        out.append(frame)
                        self.frames += 1
                    self._recent[frame] = when
        s.prev, s.phase, s.quality = prev, phase, max(-1.0, min(1.0, quality))
