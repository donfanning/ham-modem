"""HF ionospheric channel simulator: the Watterson model (CCIR Rec. 520 / ITU-R F.1487).

Two independent Rayleigh-fading paths of equal average power, the second delayed, each with a Gaussian Doppler
spectrum; plus additive Gaussian noise referenced to a 3 kHz bandwidth, an optional carrier frequency offset and
optional impulse noise::

    y(t) = Re{ e^{j2pi f_off t} [ g1(t) x_a(t) + g2(t) x_a(t - tau) ] } + n(t)

where x_a is the analytic signal of the (real, audio) input and g1, g2 are unit-power complex Gaussian processes
whose spectra are Gaussian with a two-sigma width equal to the Doppler spread.

Streaming: ``HfChannel.process(block)`` keeps all state (Hilbert FIR, delay line, Doppler generators), so the
same object impairs a live audio leg (``tools/audio_hub.py --hf``) or a whole file (``tools/hf_channel.py``).

SNR follows the ITU convention: average signal power over noise power in 3 kHz. The signal level for that
reference is either given (``signal_dbfs``) or tracked from the input while it is active.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class HfPreset:
    name: str
    delay_ms: float          # differential delay of the second path
    spread_hz: float         # two-sigma Doppler spread of each path
    description: str


PRESETS = {p.name: p for p in (
    HfPreset("awgn", 0.0, 0.0, "no fading: noise only"),
    HfPreset("good", 0.5, 0.1, "CCIR good: 0.5 ms, 0.1 Hz"),
    HfPreset("moderate", 1.0, 0.5, "CCIR moderate: 1 ms, 0.5 Hz"),
    HfPreset("poor", 2.0, 1.0, "CCIR poor: 2 ms, 1 Hz"),
    HfPreset("flutter", 0.5, 10.0, "CCIR flutter: 0.5 ms, 10 Hz"),
    HfPreset("midlat-disturbed", 2.0, 1.0, "ITU-R F.1487 mid-latitude disturbed: 2 ms, 1 Hz"),
    HfPreset("lowlat-disturbed", 6.0, 10.0, "ITU-R F.1487 low-latitude disturbed: 6 ms, 10 Hz"),
)}


def hilbert_taps(n: int = 127) -> np.ndarray:
    """Odd-length type-III Hilbert transformer, Hamming-windowed (good from ~100 Hz to fs/2 - 100 Hz at 8 kHz)."""
    k = np.arange(n) - (n - 1) // 2
    h = np.zeros(n)
    odd = k % 2 != 0
    h[odd] = 2 / (np.pi * k[odd])
    return h * np.hamming(n)


class _Doppler:
    """Unit-power complex Gaussian process with a Gaussian spectrum of two-sigma width ``spread`` Hz.

    Generated at a low rate (filtered complex white noise) and linearly interpolated to the audio rate; both
    steps vectorised, with state carried between calls."""

    def __init__(self, spread: float, fs: float, rng: np.random.Generator) -> None:
        self.rng, self.fs = rng, fs
        self.rate = max(8.0 * spread, 2.0)                    # generator rate, Hz (well above the spread)
        # A Gaussian impulse response of std s has a *power* spectrum of std 1 / (2 sqrt(2) pi s); solve for the
        # filter std (in generator samples) that gives a Doppler power spectrum of std spread / 2.
        sigma_t = self.rate / (2 * math.sqrt(2) * math.pi * (spread / 2))
        m = int(4 * sigma_t) + 1
        h = np.exp(-0.5 * (np.arange(-m, m + 1) / sigma_t) ** 2)
        self.h = h / math.sqrt(np.sum(h * h))                    # unit-power output from unit-power input
        self.hist = self._white(len(self.h) - 1)
        self.gen = self._generate(2)                             # generator samples g[k], g[k+1], ...
        self.t = 0.0                                             # position, in generator samples, of the next output

    def _white(self, n: int) -> np.ndarray:
        return (self.rng.normal(size=n) + 1j * self.rng.normal(size=n)) / math.sqrt(2)

    def _generate(self, n: int) -> np.ndarray:
        buf = np.concatenate((self.hist, self._white(n)))
        self.hist = buf[n:]
        return np.convolve(buf, self.h, "valid")

    def take(self, n: int) -> np.ndarray:
        pos = self.t + np.arange(n) * (self.rate / self.fs)
        need = int(math.floor(pos[-1])) + 2
        if need > len(self.gen):
            self.gen = np.concatenate((self.gen, self._generate(need - len(self.gen))))
        k = np.arange(len(self.gen))
        out = np.interp(pos, k, self.gen.real) + 1j * np.interp(pos, k, self.gen.imag)
        self.t = pos[-1] + self.rate / self.fs
        drop = int(math.floor(self.t))
        self.gen, self.t = self.gen[drop:], self.t - drop
        return out


class HfChannel:
    def __init__(self, fs: int = 8000, preset: str | HfPreset = "moderate", snr_db: float | None = None,
                 freq_offset_hz: float = 0.0, impulse_rate: float = 0.0, signal_dbfs: float | None = None,
                 seed: int | None = None) -> None:
        p = PRESETS[preset] if isinstance(preset, str) else preset
        self.fs, self.preset, self.snr_db = fs, p, snr_db
        self.freq_offset, self.impulse_rate = freq_offset_hz, impulse_rate
        self.rng = np.random.default_rng(seed)
        self.hil = hilbert_taps()
        self.gd = (len(self.hil) - 1) // 2                      # Hilbert group delay (real path delayed to match)
        self.hist = np.zeros(len(self.hil) - 1)
        self.delay = int(round(p.delay_ms * fs / 1000))
        self.dline = np.zeros(self.delay, complex)
        self.fading = p.spread_hz > 0
        if self.fading:
            self.g1 = _Doppler(p.spread_hz, fs, self.rng)
            self.g2 = _Doppler(p.spread_hz, fs, self.rng)
        self.n = 0
        self.sig_power = None if signal_dbfs is None else 10 ** (signal_dbfs / 10) * (32767 ** 2)
        self._tracked = 0.0

    def process(self, block: np.ndarray) -> np.ndarray:
        x = np.asarray(block, np.float64)
        buf = np.concatenate((self.hist, x))
        self.hist = buf[len(buf) - len(self.hist):]
        imag = np.convolve(buf, self.hil, "valid")
        real = buf[len(self.hil) - 1 - self.gd: len(buf) - self.gd]
        xa = real + 1j * imag                                  # analytic signal (delayed by gd)
        if self.fading:
            ext = np.concatenate((self.dline, xa))
            late = ext[:len(xa)]
            self.dline = ext[len(xa):] if self.delay else self.dline
            # Two equal-power paths: scale by 1/sqrt(2) so the average output power equals the input power.
            y = (self.g1.take(len(xa)) * xa + self.g2.take(len(xa)) * late) / math.sqrt(2)
        else:
            y = xa
        if self.freq_offset:
            t = (self.n + np.arange(len(y))) / self.fs
            y = y * np.exp(2j * np.pi * self.freq_offset * t)
        self.n += len(y)
        out = y.real
        if self.snr_db is not None:
            out = out + self._noise(x, len(out))
        return out

    def _noise(self, x: np.ndarray, n: int) -> np.ndarray:
        if self.sig_power is not None:
            ps = self.sig_power
        else:
            p = float(np.mean(x * x)) if len(x) else 0.0
            if p > 1e2:                                        # track only while a signal is present
                self._tracked = p if self._tracked == 0 else 0.98 * self._tracked + 0.02 * p
            ps = self._tracked
        if ps <= 0:
            return np.zeros(n)
        # Noise power in 3 kHz = ps / SNR; white over 0..fs/2 -> total variance scales by (fs/2) / 3000.
        var = ps / 10 ** (self.snr_db / 10) * (self.fs / 2) / 3000
        noise = self.rng.normal(0, math.sqrt(var), n)
        if self.impulse_rate:
            hits = self.rng.random(n) < self.impulse_rate / self.fs
            noise[hits] += self.rng.normal(0, 30 * math.sqrt(var), int(hits.sum()))
        return noise
