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
* a trailer of 1 + trailer_ms/10 symbols of the 1500 Hz PSK carrier (phase 0, unscaled);
* ardopcf's frequency-sampling TX filter on the result (200 Hz wide for 50 baud frames, 500 Hz for 100 baud
  4FSK, 200/500/1000/2000 Hz for 1/2/4/8-carrier PSK), then a hard clip at +-32700;
* PSK / QAM (100 baud): carriers 1500 | 1400,1600 | 1200-1800 | 800-2200 (200 Hz steps, skipping 1500) for 1/2/4/8
  carriers; each carrier's block becomes symbols (2/3/4 bits MSB first) whose low 3 bits add to the previous phase
  (index x 45 deg; 4PSK uses even indices only) and whose bit 3 (16QAM) halves the amplitude; each carrier starts
  with a reference symbol of phase 0; a symbol is 120 samples of sin(pi k/119) sin(2 pi f k/12000 + phase); the
  carriers are summed, scaled per carrier count and soft-clipped above 30000.
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


_FS_WEIGHTS = {200: {14: 0.7389, 16: 0.7389, 15: -1.0},
               500: {12: 0.10601, 18: 0.10601, 13: -0.59383, 17: -0.59383, 14: 1.0, 15: -1.0, 16: 1.0},
               1000: {10: 0.377, 20: 0.377, **{j: (1.0 if j % 2 == 0 else -1.0) for j in range(11, 20)}},
               2000: {5: 0.371, 25: 0.371, **{j: (1.0 if j % 2 == 0 else -1.0) for j in range(6, 25)}}}
_FS_H: dict[int, np.ndarray] = {}


def _fs_impulse(width: int) -> np.ndarray:
    """Impulse response of ardopcf's TX frequency-sampling filter (comb 1 - r^120 z^-120, then 1 - r^2 z^-2, then
    resonators at the 100 Hz slots with ardopcf's transition weights, gain 1/120); the comb zeros cancel the
    resonator poles, so it is an FIR of 123 taps."""
    if width not in _FS_H:
        r, n = 0.9995, 120
        x = np.zeros(200)
        x[0] = 1.0
        zin = x - r ** n * np.concatenate((np.zeros(n), x[:-n]))
        comb = zin - r * r * np.concatenate((np.zeros(2), zin[:-2]))
        h = np.zeros(len(x))
        for j, w in _FS_WEIGHTS[width].items():
            c = 2 * r * math.cos(2 * math.pi * j / n)
            y1 = y2 = 0.0
            for i, v in enumerate(comb):
                y0 = v + c * y1 - r * r * y2
                h[i] += w * y0
                y2, y1 = y1, y0
        _FS_H[width] = h[:n + 3] / n
    return _FS_H[width]


def _bandpass(x: np.ndarray, width: int) -> np.ndarray:
    """ardopcf's TX filter (200/500/1000/2000 Hz centred on 1500 Hz); like ardopcf, the first 60 output samples
    (half the filter length) are dropped and nothing is flushed after the input ends."""
    y = np.convolve(x, _fs_impulse(width))
    return np.clip(y[60:len(x)], -32700, 32700)


def _trailer(trailer_ms: int) -> np.ndarray:
    """ardopcf's AddTrailer: 1 + trailer/10 symbols of the (unscaled) 1500 Hz PSK template, phase 0."""
    return np.tile(_TRAILER_SYM, 1 + trailer_ms // 10)


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
    x = np.concatenate((head, body, _trailer(trailer_ms)))
    x = _bandpass(x, 200 if info.baud == 50 else 500)
    return np.clip(np.round(x), -32768, 32767).astype(np.int16)


CARRIERS = {1: (1500.0,), 2: (1400.0, 1600.0), 4: (1200.0, 1400.0, 1600.0, 1800.0),
            8: (800.0, 1000.0, 1200.0, 1400.0, 1600.0, 1800.0, 2000.0, 2200.0)}
_SCALE = {(1, False): 1.2, (2, False): 0.65, (2, True): 0.67, (4, False): 0.4, (4, True): 0.4,
          (8, False): 0.25, (8, True): 0.27, (1, True): 1.2}
BITS = {"4PSK": 2, "8PSK": 3, "16QAM": 4}
_PSK_ENV = np.sin(np.pi * np.arange(120) / 119)
_TRAILER_SYM = np.round(AMP * _PSK_ENV * np.sin(2 * np.pi * 1500 * np.arange(120) / FS))


def psk_symbols(block: bytes, mod: str) -> list[int]:
    """Differential phase/magnitude symbols for one carrier's block (excluding the reference symbol)."""
    bits_per = BITS[mod]
    step = 2 if mod == "4PSK" else 1
    count = len(block) * 8 // bits_per
    value = int.from_bytes(block, "big")
    total = len(block) * 8
    out: list[int] = []
    prev = 0
    for i in range(count):
        sym = (value >> (total - bits_per * (i + 1))) & ((1 << bits_per) - 1)
        prev = (prev + sym * step) & 7
        out.append(prev | (sym & 0x08))
        prev &= 7
    return out


def soft_clip(x: np.ndarray) -> np.ndarray:
    over, under = x > 30000, x < -30000
    y = x.copy()
    y[over] = np.minimum(32700, 30000 + 20 * np.sqrt(x[over] - 30000))
    y[under] = np.maximum(-32700, -30000 - 20 * np.sqrt(-(x[under] + 30000)))
    return y


def modulate_psk(encoded: bytes, leader_ms: int = 240, trailer_ms: int = 20) -> np.ndarray:
    info = codec.FRAMES[encoded[0]]
    if info.mod not in BITS:
        raise ValueError(f"{info.name}: not a PSK/QAM frame")
    ncar = info.carriers
    blen = info.data_len + 3 + info.rs_len
    k = np.arange(120)
    tpl = {f: [AMP * _PSK_ENV * np.sin(2 * np.pi * f * k / FS + p * np.pi / 4) for p in range(8)]
           for f in CARRIERS[ncar]}
    per_car = []
    for c in range(ncar):
        block = encoded[2 + c * blen:2 + (c + 1) * blen]
        per_car.append([0] + psk_symbols(block, info.mod))
    nsym = len(per_car[0])
    body = np.zeros(nsym * 120)
    for c, f in enumerate(CARRIERS[ncar]):
        for i, sym in enumerate(per_car[c]):
            amp = 0.5 if sym & 0x08 else 1.0
            body[i * 120:(i + 1) * 120] += amp * tpl[f][sym & 7]
    body = soft_clip(body * _SCALE[(ncar, info.mod == "16QAM")])
    type_part = []
    for j in range(2):
        b = encoded[j]
        type_part += [(b >> (2 * (3 - kk))) & 3 for kk in range(4)] + [codec.type_parity(encoded[0])]
    head = np.concatenate((leader_and_sync(leader_ms), _fsk_symbols(type_part, 50)))
    x = np.concatenate((head, body, _trailer(trailer_ms)))
    x = _bandpass(x, {1: 200, 2: 500, 4: 1000, 8: 2000}[ncar])
    return np.clip(np.round(x), -32768, 32767).astype(np.int16)


def modulate(encoded: bytes, leader_ms: int = 240, trailer_ms: int = 20) -> np.ndarray:
    """Waveform for any supported encoded frame."""
    info = codec.FRAMES[encoded[0]]
    if info.mod in BITS:
        return modulate_psk(encoded, leader_ms, trailer_ms)
    return modulate_4fsk(encoded, leader_ms, trailer_ms)


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
