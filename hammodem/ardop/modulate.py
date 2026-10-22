"""ARDOP transmit side: frame encoders and the 4FSK waveform (leader, sync, frame type, data), at 12 kHz.

Ported from ardopcf (MIT; Muething, Wiseman, LaRue) -- ``ARDOPC.c`` Encode* and ``Modulate.c`` /
``CalcTemplates.c``:

* leader: 20 ms symbols of a 1475 + 1525 Hz two-tone (amplitude 0.55 x 26000), sign alternating per symbol; the
  last leader symbol repeats the previous sign (the sync);
* frame type: two bytes (type, type ^ session ID), each four 2-bit 4FSK symbols MSB first + a parity symbol,
  50 baud on 1425/1475/1525/1575 Hz;
* 4FSK data: each encoded byte as four symbols, MSB first, at 50 baud (tones above) or 100 baud
  (1350/1450/1550/1650 Hz), amplitude 1.1 x 26000; every other symbol is negated (the templates start at phase 0,
  so this keeps the waveform continuous at symbol boundaries);
* a band-pass on the result (ardopcf uses a frequency-sampling filter: 200 Hz wide for 50 baud frames, 500 Hz for
  100 baud; an ordinary FIR here).
"""

from __future__ import annotations

import math

import numpy as np

from . import codec
from .codec import SAMPLE_RATE as FS

AMP = 26000
FSK50 = (1425.0, 1475.0, 1525.0, 1575.0)
FSK100 = (1350.0, 1450.0, 1550.0, 1650.0)


def _template(freq: float, n: int) -> np.ndarray:
    return AMP * 1.1 * np.sin(2 * np.pi * freq / FS * np.arange(n))


_T50 = [_template(f, 240) for f in FSK50]
_T100 = [_template(f, 120) for f in FSK100]
_k = np.arange(240)
_LEADER = AMP * 0.55 * (np.sin(1475 / 1500 * _k / 8 * 2 * np.pi) - np.sin(1525 / 1500 * _k / 8 * 2 * np.pi))


def leader_and_sync(leader_ms: int) -> np.ndarray:
    n = max(2, leader_ms // 20)
    sign = -1 if n & 1 else 1
    out = []
    for i in range(n):
        s = sign if i != n - 1 else -sign
        out.append(s * _LEADER)
        sign = -sign
    return np.concatenate(out)


def _fsk_symbols(symbols: list[int], baud: int, start_index: int = 0) -> np.ndarray:
    tpl = _T50 if baud == 50 else _T100
    return np.concatenate([tpl[s] * (1 if ((start_index + i) & 1) == 0 else -1) for i, s in enumerate(symbols)])


def _bandpass(x: np.ndarray, width: int) -> np.ndarray:
    lo, hi = 1500 - width / 2 - 100, 1500 + width / 2 + 100
    n = 241
    t = np.arange(n) - (n - 1) / 2
    h = (2 * hi / FS * np.sinc(2 * hi / FS * t) - 2 * lo / FS * np.sinc(2 * lo / FS * t)) * np.hamming(n)
    return np.convolve(x, h)[(n - 1) // 2:(n - 1) // 2 + len(x)]


def modulate_4fsk(encoded: bytes, leader_ms: int = 240, trailer_ms: int = 20) -> np.ndarray:
    """Waveform for an encoded 4FSK frame (``encoded[0:2]`` = frame type bytes, then the per-carrier data)."""
    info = codec.FRAMES[encoded[0]]
    if info.mod != "4FSK" or info.baud not in (50, 100):
        raise ValueError(f"{info.name}: not a 50/100 baud 4FSK frame")
    type_part = []
    for j in range(2):
        b = encoded[j]
        type_part += [(b >> (2 * (3 - k))) & 3 for k in range(4)] + [codec.type_parity(encoded[0])]
    # Frame-type symbols alternate sign by their index within the 10 (5 * j + k).
    head = np.concatenate((leader_and_sync(leader_ms), _fsk_symbols(type_part, 50)))
    data_syms = [(b >> (2 * (3 - k))) & 3 for b in encoded[2:] for k in range(4)]
    body = _fsk_symbols(data_syms, info.baud) if data_syms else np.zeros(0)
    x = np.concatenate((head, body, np.zeros(int(FS * trailer_ms / 1000))))
    x = _bandpass(x, 200 if info.baud == 50 else 500)
    return np.clip(np.round(x), -32768, 32767).astype(np.int16)


# ---------------------------------------------------------------------------------------------- encoders

def encode_fsk_data(frame_type: int, data: bytes, session: int) -> bytes:
    """[type, type ^ session] + per carrier: count, data padded to data_len, CRC16 (^ type), RS parity."""
    info = codec.FRAMES[frame_type]
    if info.baud == 600:
        raise NotImplementedError("600 baud FM frames")
    out = bytes((frame_type, frame_type ^ session))
    pos = 0
    for _ in range(info.carriers):
        chunk = data[pos:pos + info.data_len]
        pos += len(chunk)
        block = bytes((len(chunk),)) + chunk + bytes(info.data_len - len(chunk))
        out += codec.rs_append(codec.append_crc(block, frame_type), info.rs_len)
    return out


def encode_conreq(caller: str, target: str, bandwidth: str = "500M") -> bytes:
    t = codec.CONREQ[bandwidth]
    return bytes((t, t ^ 0xFF)) + codec.rs_append(codec.station_bytes(caller) + codec.station_bytes(target), 4)


def encode_ping(caller: str, target: str) -> bytes:
    return bytes((codec.PING, codec.PING ^ 0xFF)) + codec.rs_append(
        codec.station_bytes(caller) + codec.station_bytes(target), 4)


def encode_idframe(call: str, grid: str = "") -> bytes:
    return bytes((codec.IDFRAME, codec.IDFRAME ^ 0xFF)) + codec.rs_append(
        codec.station_bytes(call) + codec.packed6(grid.upper()), 4)


def encode_control(frame_type: int, session: int) -> bytes:
    return bytes((frame_type, frame_type ^ session))


def encode_conack(bandwidth: int, received_leader_ms: int, session: int) -> bytes:
    t = codec.CONACK[bandwidth]
    timing = min(255, received_leader_ms // 10) if 0 <= received_leader_ms <= 2550 else 0
    return bytes((t, t ^ session, timing, timing, timing))


def encode_dataack(quality: int, session: int, nak: bool = False) -> bytes:
    q = max(0, min(100, quality) // 2 - 19)
    t = q if nak else codec.DATAACK_MIN + q
    return bytes((t, t ^ session))
