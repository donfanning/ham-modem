"""Packet modems: each turns AX.25 frames into audio samples and audio samples back into frames.

Every modem pair has the same surface. Modulator: ``modulate(frames, txdelay, txtail) -> int16 samples`` and
``flags_for(seconds)``. Demodulator: ``feed(samples) -> [frame bytes]``, ``dcd``, ``bad_fcs``.
"""

from __future__ import annotations

MODEMS = ("afsk1200", "afsk300", "g3ruh9600")


def make(name: str, fs: int):
    """(modulator, demodulator) for ``name`` at sample rate ``fs``."""
    if name == "g3ruh9600":
        from .g3ruh import G3ruhDemodulator, G3ruhModulator  # noqa: PLC0415
        return G3ruhModulator(fs), G3ruhDemodulator(fs)
    from .afsk import PROFILES, AfskDemodulator, AfskModulator  # noqa: PLC0415
    if name not in PROFILES:
        raise ValueError(f"unknown modem {name!r}; choose from {', '.join(MODEMS)}")
    return AfskModulator(PROFILES[name], fs), AfskDemodulator(PROFILES[name], fs)
