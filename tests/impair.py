"""Channel impairments for modem tests: noise, radio emphasis, sample-clock drift and RTP frame loss."""

import numpy as np


def noise(x, rms, rng):
    return x + rng.normal(0, rms, len(x))


def deemphasis(x, fs=8000, fc=300.0):
    """First-order low-pass, about -6 dB/octave above ``fc`` (a de-emphasised FM receiver)."""
    a = np.exp(-2 * np.pi * fc / fs)
    y = np.empty(len(x))
    s = 0.0
    for i, v in enumerate(x):
        s = a * s + (1 - a) * v
        y[i] = s
    return y * (np.std(x) / np.std(y))


def preemphasis(x, k=0.6):
    y = np.append(x[0], x[1:] - k * x[:-1])
    return y * (np.std(x) / np.std(y))


def drift(x, ppm):
    """Resample as if the receiver's clock ran ``ppm`` fast."""
    t = np.arange(0, len(x) - 1, 1 + ppm * 1e-6)
    return np.interp(t, np.arange(len(x)), x)


def frame_loss(x, p, rng, frame=160):
    """Drop 20 ms RTP frames with probability ``p``, concealed by repeating the previous frame."""
    x = x.copy()
    for i in range(frame, len(x), frame):
        if rng.random() < p:
            x[i:i + frame] = x[i - frame:i][:len(x[i:i + frame])]
    return x


def to_pcm(x):
    return np.clip(np.round(x), -32768, 32767).astype(np.int16)
