"""Burst acquisition shared by the HF keyboard modems.

The HF modems are burst-oriented: a transmission starts with a shaped leader,
lasts a fixed number of samples, and is followed by silence or a short control
reply.  Audio arrives in arbitrary block sizes, so the link layer needs to
find the start of a burst before handing it to the demodulator.
"""

from __future__ import annotations

import numpy as np


def find_onset(audio: np.ndarray, fs: int = 8000, win_ms: float = 2.0,
               min_run_ms: float = 8.0, threshold: float = 8.0) -> int | None:
    """Return the first sample of a burst, or ``None``.

    A low threshold is used deliberately: Clover and PACTOR both begin with a
    shaped ramp, and a peak-relative gate of a few percent would start in the
    middle of the leader and destroy symbol alignment.  For a buffer that
    already begins with signal, 0 is returned so the leading edge is kept.
    """
    x = np.asarray(audio, dtype=np.float64)
    if x.size == 0:
        return None
    peak = float(np.max(np.abs(x)))
    if peak <= 1e-9:
        return None

    win = max(1, int(round(fs * win_ms / 1000.0)))
    head = float(np.max(np.abs(x[:win])))
    if head > peak * 0.005:
        return 0

    # Silence at the start: locate the first low-level signal run.  A low
    # absolute gate is intentional; the first samples of a shaped burst can
    # be only one or two LSBs.  Returning one sample before the first hit
    # keeps the zero-valued first sample of a Hann-shaped Clover pulse.
    span = max(1, int(round(fs * 0.01)))
    hits_needed = max(2, span // 10)
    noise = float(np.percentile(np.abs(x), 1.0))
    level = max(peak * 1e-5, noise * 2.0, 0.5)
    above = np.abs(x) > level

    first_hit = None
    for idx in np.flatnonzero(above):
        if int(np.count_nonzero(above[idx:idx + span])) >= hits_needed:
            first_hit = int(idx)
            break
    if first_hit is None:
        return None
    return max(0, first_hit - 1)


def pop_burst(buffer: np.ndarray, length: int, fs: int = 8000) -> tuple[np.ndarray, np.ndarray] | None:
    """Remove one ``length``-sample burst from ``buffer``.

    Returns ``(burst, remainder)``.  If no complete burst is available yet,
    returns ``None`` and leaves the buffer untouched.
    """
    onset = find_onset(buffer, fs)
    if onset is None:
        return None
    end = onset + length
    if end > len(buffer):
        return None
    return buffer[onset:end], buffer[end:]


def trim_silence(audio: np.ndarray, fs: int = 8000) -> np.ndarray:
    """Return the signal with leading and trailing silence removed (best effort)."""
    start = find_onset(audio, fs)
    if start is None:
        return np.zeros(0, dtype=audio.dtype)
    x = np.asarray(audio)[start:]
    rev = find_onset(x[::-1], fs)
    if rev is not None and rev > 0:
        x = x[:-rev] if rev < len(x) else x
    return x
