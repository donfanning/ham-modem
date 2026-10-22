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

from ..fx25 import Fx25Receiver
from ..fx25 import burst_bits as fx25_burst_bits
from ..il2p import Il2pReceiver
from ..il2p import burst_levels as il2p_burst_levels
from .hdlc import HdlcDecoder, NrziDecoder, NrziEncoder, extend_bits, span_mask


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
        self.fx25 = 0                     # FX.25 check bytes per frame (16/32/64), 0 = plain AX.25
        self.il2p: bool | None = None     # send IL2P instead (True: max FEC, False: normal FEC)
        self.phase = 0.0
        self.bits_sent = 0                # bit k starts at sample k * fs / baud, kept exact in integers
        self.samples_sent = 0

    def flags_for(self, seconds: float) -> int:
        return max(1, math.ceil(seconds * self.p.baud / 8))

    def modulate_bits(self, bits: list[int]) -> np.ndarray:
        """NRZI-encode raw line bits and render them, keeping phase and bit timing across calls."""
        return self.modulate_levels(self.nrzi.encode(bits))

    def modulate_levels(self, line: list[int]) -> np.ndarray:
        """Render line levels directly (1 = mark), e.g. IL2P, which uses no NRZI."""
        levels = np.array(line, dtype=np.int8)
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
        if self.il2p is not None:
            return self.modulate_levels(il2p_burst_levels(frames, self.flags_for(txdelay), self.flags_for(txtail),
                                                          self.il2p))
        bits = fx25_burst_bits(frames, self.flags_for(txdelay), self.flags_for(txtail), self.fx25)
        return self.modulate_bits(bits)


class _Slicer:
    """One decision threshold: PLL bit clock, NRZI decoder, HDLC receiver and a carrier-quality score."""

    __slots__ = ("gain", "phase", "prev", "nrzi", "hdlc", "fx25", "il2p", "quality", "run", "since")

    def __init__(self, gain: float) -> None:
        self.gain = gain
        self.phase = 0.0                  # -1..1; a bit is sampled when it wraps past +1 (mid-bit)
        self.prev = 0
        self.nrzi = NrziDecoder()
        self.hdlc = HdlcDecoder()
        self.fx25 = Fx25Receiver()
        self.il2p = Il2pReceiver()
        self.quality = 0.0
        self.run = 0                      # bits since the last transition
        self.since = 1 << 30              # bits since the last erased bit


class AfskDemodulator:
    """Samples in, good-FCS AX.25 frames out. ``dcd`` tracks whether a packet signal is present."""

    INERTIA_LOCKED = 0.74             # PLL phase kept on a transition while receiving a frame
    INERTIA_SEARCH = 0.50
    DCD_THRESHOLD = 0.4

    def __init__(self, profile: AfskProfile = BELL202, fs: int = 8000, energy_dcd_dbfs: float | None = -45.0) -> None:
        """``energy_dcd_dbfs``: also report DCD whenever in-band power exceeds this level. A VoIP channel idles at
        digital silence, so this reacts within one block (the quality score needs ~40 ms) and treats speech in
        the bridge as busy. Use None on a noisy RF-like channel."""
        self.p, self.fs = profile, fs
        self.energy_dcd_dbfs = energy_dcd_dbfs
        self.energy_busy = False
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
        self._erase_delay = (nbp - 1) + (nwin - 1) + (nlp - 1)   # an input sample reaches this far into the output
        self._spans: list[tuple[int, int]] = []
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
        return self.energy_busy or any(s.quality > self.DCD_THRESHOLD for s in self.slicers)

    @property
    def bad_fcs(self) -> int:
        return max(s.hdlc.bad_fcs for s in self.slicers)

    def feed(self, pcm: np.ndarray | bytes, erased: bool = False) -> list[bytes]:
        """``erased``: these samples conceal lost packets (bits from them become FX.25 erasures)."""
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            pcm = np.frombuffer(pcm, dtype="<i2")
        if erased:
            self._spans.append((self.samples, self.samples + len(pcm) + self._erase_delay))
        self._spans = [sp for sp in self._spans if sp[1] > self.samples]
        self._mask = span_mask(self._spans, self.samples, len(pcm)).tolist() if self._spans else None
        x = self._bp(np.asarray(pcm, dtype=np.float64) / 32768.0)
        if self.energy_dcd_dbfs is not None and len(x):
            self.energy_busy = 10 * np.log10(np.mean(x * x) * 2 + 1e-12) > self.energy_dcd_dbfs
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

    def _emit(self, frame: bytes, when: int, out: list[bytes]) -> None:
        """Report a frame once, whichever slicer -- or the plain/FX.25 path -- decoded it first."""
        seen = self._recent.get(frame)
        if seen is None or when - seen > self.fs:
            out.append(frame)
            self.frames += 1
        self._recent[frame] = when

    @property
    def fx25_corrected(self) -> int:
        return max(s.fx25.corrected for s in self.slicers)

    def _run(self, s: _Slicer, levels: list[bool], base: int, out: list[bytes]) -> None:
        step, prev, phase, quality, run = self.step, s.prev, s.phase, s.quality, s.run
        nrzi, hdlc = s.nrzi, s.hdlc
        bits: list[int] = []
        mask = self._mask
        flags: list[bool] = []
        raw: list[int] = []
        for i, level in enumerate(levels):
            level = 1 if level else 0
            if level != prev:
                # A transition should land on a bit boundary (phase near 0); score how close it was.
                quality = quality * 0.96 + (0.04 if abs(phase) < 0.35 else -0.04)
                phase *= self.INERTIA_LOCKED if hdlc.active else self.INERTIA_SEARCH
                prev = level
                run = 0
            phase += step
            if phase >= 1.0:
                phase -= 2.0
                run += 1
                if run > 7:                   # bit stuffing caps NRZI runs at 7 bits: no packet signal here
                    quality = max(-1.0, quality - 0.1)
                raw.append(level)
                bit = nrzi.decode(level)
                bits.append(bit)
                if mask is not None:
                    flags.append(mask[i])
                frame = hdlc.push(bit)
                if frame is not None:
                    self._emit(frame, base + i, out)
        s.prev, s.phase, s.quality, s.run = prev, phase, max(-1.0, min(1.0, quality)), run
        if flags:
            ext, s.since = extend_bits(np.array(flags), s.since, 1)
        else:
            ext, s.since = None, s.since + len(bits)
        for frame in s.fx25.feed_bits(bits, ext):
            self._emit(frame, base + len(levels), out)
        for frame in s.il2p.feed_bits(raw):
            self._emit(frame, base + len(levels), out)
