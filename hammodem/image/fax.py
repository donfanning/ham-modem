"""HF FAX (Radiofax / WEFAX) modem — ITU-R F.7 compatible.

Waveform: FM audio, 1500 Hz = black (0), 2300 Hz = white (255).
Center frequency 1900 Hz, deviation ±400 Hz.
Grayscale only; the image is represented as a numpy uint8 array (H × W).

Typical configurations:
    IOC 576, 120 LPM — standard meteorological fax
    IOC 576,  60 LPM — slower (less frequency resolution required)
    IOC 288, 120 LPM — compressed width
"""

import numpy as np
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FS        = 8000       # sample rate (Hz)
FREQ_BLACK = 1500.0    # Hz — black / sync level
FREQ_WHITE = 2300.0    # Hz — peak white
FREQ_RANGE = FREQ_WHITE - FREQ_BLACK   # 800 Hz
FREQ_SYNC  = 1500.0    # sync pulse frequency (= black)
FREQ_START = 1000.0    # phasing / start-tone frequency

# Sync pulse: 5 % of line period
SYNC_FRAC  = 0.050
# Phasing lines prepended and appended to the image
PHASING_LINES = 5

import math
_IOC_WIDTHS = {576: int(round(576 * math.pi)),   # 1810
               288: int(round(288 * math.pi))}    # 904


# ---------------------------------------------------------------------------
# Low-level FM helpers
# ---------------------------------------------------------------------------

def _fm_encode(freqs: np.ndarray, fs: int = FS,
               init_phase: float = 0.0) -> tuple[np.ndarray, float]:
    """Integrate instantaneous frequency array → audio samples.

    Returns (audio, final_phase) so successive segments stay phase-continuous.
    """
    phase = init_phase + np.cumsum(2.0 * np.pi * freqs / fs)
    audio = np.sin(phase)
    return audio, float(phase[-1]) if len(phase) else init_phase


def _fm_demodulate(audio: np.ndarray, fc: float = 1900.0,
                   fs: int = FS) -> np.ndarray:
    """FM discriminator via analytic-signal complex baseband differentiation.

    Uses an FFT-based Hilbert transform to form the analytic signal so that
    the real signal's negative-frequency image is fully suppressed before
    mixing to baseband.  Returns instantaneous frequency estimate (Hz).
    """
    x = audio.astype(np.float64) / 32768.0
    n = len(x)
    # Analytic signal via FFT (numpy-only Hilbert transform)
    X = np.fft.fft(x)
    H = np.zeros(n)
    H[0] = 1.0
    if n % 2 == 0:
        H[1:n // 2] = 2.0
        H[n // 2]   = 1.0
    else:
        H[1:(n + 1) // 2] = 2.0
    z = np.fft.ifft(X * H)
    # Mix down to baseband
    t  = np.arange(n) / fs
    bb = z * np.exp(-2j * np.pi * fc * t)
    # Instantaneous frequency via complex differentiation
    diff     = bb[1:] * np.conj(bb[:-1])
    freq_dev = np.angle(diff) * fs / (2.0 * np.pi)
    freq     = fc + freq_dev
    return np.concatenate([[freq[0]], freq])


# ---------------------------------------------------------------------------
# Line codec helpers
# ---------------------------------------------------------------------------

def _line_to_audio(row: np.ndarray, line_samples: int, sync_samples: int,
                   phase: float, fs: int = FS) -> tuple[np.ndarray, float]:
    """Encode one image row → one scan line of audio.

    row: 1-D uint8 pixel array (values 0-255, 0=black, 255=white).
    Returns (audio, final_phase).
    """
    image_samples = line_samples - sync_samples
    # Sync pulse
    sync_freqs = np.full(sync_samples, FREQ_SYNC)
    sync_audio, phase = _fm_encode(sync_freqs, fs, phase)
    # Image: interpolate pixel array to image_samples
    src = np.linspace(0, len(row) - 1, image_samples)
    pixels = np.interp(src, np.arange(len(row)), row.astype(np.float64))
    img_freqs = FREQ_BLACK + (pixels / 255.0) * FREQ_RANGE
    img_audio, phase = _fm_encode(img_freqs, fs, phase)
    return np.concatenate([sync_audio, img_audio]), phase


def _audio_to_line(freq_track: np.ndarray, line_samples: int,
                   sync_samples: int, width: int) -> np.ndarray:
    """Extract pixel values from one scan line's frequency track.

    freq_track: instantaneous frequency for this line (length = line_samples).
    Returns uint8 pixel array of length `width`.
    """
    image_part = freq_track[sync_samples:]
    pixel_freqs = np.clip(image_part, FREQ_BLACK, FREQ_WHITE)
    pixels_float = (pixel_freqs - FREQ_BLACK) / FREQ_RANGE * 255.0
    # Downsample to requested width
    src = np.linspace(0, len(pixels_float) - 1, width)
    return np.clip(np.interp(src, np.arange(len(pixels_float)), pixels_float),
                   0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# Public encoder
# ---------------------------------------------------------------------------

class HfFaxEncoder:
    """Encode a grayscale image to HF FAX audio.

    Parameters
    ----------
    lpm : int
        Lines per minute.  120 is the standard meteorological rate.
    ioc : int
        Index of Cooperation.  576 or 288.
    fs : int
        Sample rate.  Must be 8000 for VoIP use.
    """

    def __init__(self, lpm: int = 120, ioc: int = 576, fs: int = FS) -> None:
        self.lpm          = lpm
        self.ioc          = ioc
        self.fs           = fs
        self._line_samples = int(round(fs * 60.0 / lpm))
        self._sync_samples = max(1, int(round(SYNC_FRAC * self._line_samples)))
        self._width        = _IOC_WIDTHS.get(ioc, int(round(ioc * np.pi)))

    # -- properties -----------------------------------------------------------

    @property
    def line_samples(self) -> int:
        return self._line_samples

    @property
    def sync_samples(self) -> int:
        return self._sync_samples

    @property
    def image_samples(self) -> int:
        return self._line_samples - self._sync_samples

    @property
    def width(self) -> int:
        return self._width

    # -- encode ---------------------------------------------------------------

    def encode(self, image: np.ndarray) -> np.ndarray:
        """Encode a H×W uint8 grayscale image to int16 audio at `fs` Hz.

        The image is scaled to self.width columns; height is preserved.
        Prepends/appends `PHASING_LINES` phasing lines.
        """
        if image.ndim != 2:
            raise ValueError("image must be 2-D (H×W) grayscale")
        image = image.astype(np.uint8)
        h, w  = image.shape

        segments: list[np.ndarray] = []
        phase = 0.0

        # Phasing lines (alternating black / white columns)
        phase_row = np.tile([0, 255], self._width // 2 + 1)[:self._width].astype(np.uint8)
        for _ in range(PHASING_LINES):
            seg, phase = _line_to_audio(phase_row, self._line_samples,
                                        self._sync_samples, phase, self.fs)
            segments.append(seg)

        # Image rows (scale width to self._width)
        for r in range(h):
            row = image[r]
            if w != self._width:
                src = np.linspace(0, w - 1, self._width)
                row = np.interp(src, np.arange(w), row.astype(np.float64)).astype(np.uint8)
            seg, phase = _line_to_audio(row, self._line_samples,
                                        self._sync_samples, phase, self.fs)
            segments.append(seg)

        # Stop / phasing trailer (white lines)
        white_row = np.full(self._width, 255, dtype=np.uint8)
        for _ in range(PHASING_LINES):
            seg, phase = _line_to_audio(white_row, self._line_samples,
                                        self._sync_samples, phase, self.fs)
            segments.append(seg)

        audio = np.concatenate(segments)
        return np.clip(audio * 32767, -32768, 32767).astype(np.int16)


# ---------------------------------------------------------------------------
# Public decoder
# ---------------------------------------------------------------------------

class HfFaxDecoder:
    """Decode HF FAX audio to a grayscale image.

    Parameters
    ----------
    lpm : int
        Expected lines per minute.
    ioc : int
        Expected IOC (defines output image width).
    fs : int
        Sample rate.
    """

    def __init__(self, lpm: int = 120, ioc: int = 576, fs: int = FS) -> None:
        self.lpm           = lpm
        self.ioc           = ioc
        self.fs            = fs
        self._line_samples = int(round(fs * 60.0 / lpm))
        self._sync_samples = max(1, int(round(SYNC_FRAC * self._line_samples)))
        self._width        = _IOC_WIDTHS.get(ioc, int(round(ioc * np.pi)))

    @property
    def width(self) -> int:
        return self._width

    def decode(self, audio: np.ndarray) -> Optional[np.ndarray]:
        """Decode int16 (or float64) audio → uint8 grayscale image array.

        Returns None if no sync lines are detected.
        The returned array has shape (H, self.width) where H is the number
        of detected image lines (phasing lines stripped).
        """
        freq = _fm_demodulate(audio, fc=(FREQ_BLACK + FREQ_WHITE) / 2, fs=self.fs)

        # Detect sync pulses: look for runs of samples near FREQ_SYNC
        ls   = self._line_samples
        ss   = self._sync_samples
        tol  = 80.0   # Hz tolerance for sync detection

        near_sync = (np.abs(freq - FREQ_SYNC) < tol).astype(np.uint8)
        # Find start of each sync pulse using a running-sum detector
        # A sync pulse must span at least 60% of sync_samples
        min_run = max(1, int(0.60 * ss))
        line_starts = _find_sync_starts(near_sync, min_run, ls)

        if len(line_starts) < 2:
            return None

        rows = []
        for start in line_starts:
            end = start + ls
            if end > len(freq):
                break
            row = _audio_to_line(freq[start:end], ls, ss, self._width)
            rows.append(row)

        if not rows:
            return None

        image = np.stack(rows, axis=0)
        # Strip phasing lines from head and tail (cap at half minus one row)
        n = len(image)
        skip = min(PHASING_LINES, (n - 1) // 2)
        image = image[skip:max(skip + 1, n - skip)]
        return image if len(image) > 0 else None


def _find_sync_starts(near_sync: np.ndarray, min_run: int,
                      line_samples: int) -> list[int]:
    """Find sample indices where sync pulses begin.

    Uses a sliding-window approach: at each candidate position, check
    if a sufficient fraction of samples are at the sync frequency.
    After a detection, advance by (line_samples - min_run) to avoid
    double-counting within the same line.
    """
    n       = len(near_sync)
    starts  = []
    pos     = 0
    advance = max(1, line_samples - min_run)

    while pos + min_run <= n:
        window = near_sync[pos:pos + min_run]
        if np.sum(window) >= int(0.80 * min_run):
            starts.append(pos)
            pos += advance
        else:
            pos += 1

    return starts


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------

class HfFax:
    """Combined encoder / decoder for HF FAX.

    Usage::

        fax = HfFax(lpm=120, ioc=576)
        audio  = fax.encode(image_array)   # H×W uint8 → int16 audio
        recovered = fax.decode(audio)       # int16 audio → H×W uint8 or None
    """

    def __init__(self, lpm: int = 120, ioc: int = 576, fs: int = FS) -> None:
        self._enc = HfFaxEncoder(lpm, ioc, fs)
        self._dec = HfFaxDecoder(lpm, ioc, fs)
        self.lpm  = lpm
        self.ioc  = ioc
        self.fs   = fs

    @property
    def width(self) -> int:
        return self._enc.width

    @property
    def line_samples(self) -> int:
        return self._enc.line_samples

    def encode(self, image: np.ndarray) -> np.ndarray:
        return self._enc.encode(image)

    def decode(self, audio: np.ndarray) -> Optional[np.ndarray]:
        return self._dec.decode(audio)
