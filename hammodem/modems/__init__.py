"""Packet modems: each turns AX.25 frames into audio samples and audio samples back into frames.

Every modem pair has the same surface. Modulator: ``modulate(frames, txdelay, txtail) -> int16 samples`` and
``flags_for(seconds)``. Demodulator: ``feed(samples) -> [frame bytes]``, ``dcd``, ``bad_fcs``.
"""

from __future__ import annotations

MODEMS = ("afsk1200", "afsk300", "psk2400", "psk4800", "g3ruh9600", "g3ruh19200", "g3ruh38400", "pam19200", "pam57600", "qam38400",
          "qam51200", "datac0", "datac1", "datac3", "datac4", "datac13",
          "ninobpsk300", "ninobpsk1200", "ninopsk600", "ninopsk3600")


def default_rate(name: str) -> int:
    """The narrowest linear leg rate each modem fits."""
    return {"g3ruh9600": 16000, "g3ruh19200": 48000, "g3ruh38400": 48000, "pam19200": 16000,
            "pam57600": 48000, "qam38400": 16000, "qam51200": 16000}.get(name, 8000)


def make(name: str, fs: int):
    """(modulator, demodulator) for ``name`` at sample rate ``fs``."""
    if name.startswith("datac"):
        from .datac import DatacDemodulator, DatacModulator  # noqa: PLC0415
        return DatacModulator(name, fs), DatacDemodulator(name, fs)
    if name.startswith("nino"):
        from .psk import PROFILES as PSKP, PskDemodulator, PskModulator  # noqa: PLC0415
        p = PSKP[name]
        mod = PskModulator(p, fs)
        mod.il2p, mod.il2p_crc = True, True         # IL2Pc framing (max FEC + trailing CRC)
        return mod, PskDemodulator(p, fs, il2p_crc=True)
    if name.startswith("psk"):
        from .psk import PROFILES as PSKP, PskDemodulator, PskModulator  # noqa: PLC0415
        return PskModulator(PSKP[name], fs), PskDemodulator(PSKP[name], fs)
    if name.startswith("qam"):
        from .qam import RATES as QRATES, QamDemodulator, QamModulator  # noqa: PLC0415
        return QamModulator(fs, QRATES[name]), QamDemodulator(fs, QRATES[name])
    if name.startswith("pam"):
        from .pam import RATES, PamDemodulator, PamModulator  # noqa: PLC0415
        baud = RATES[name][0]
        return PamModulator(fs, baud), PamDemodulator(fs, baud)
    if name.startswith("g3ruh"):
        from .g3ruh import G3ruhDemodulator, G3ruhModulator  # noqa: PLC0415
        baud = int(name[5:])
        return G3ruhModulator(fs, baud), G3ruhDemodulator(fs, baud)
    from .afsk import PROFILES, AfskDemodulator, AfskModulator  # noqa: PLC0415
    if name not in PROFILES:
        raise ValueError(f"unknown modem {name!r}; choose from {', '.join(MODEMS)}")
    return AfskModulator(PROFILES[name], fs), AfskDemodulator(PROFILES[name], fs)
