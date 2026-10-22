"""ctypes wrapper for libcodec2's raw-data OFDM modes (FreeDV "datac"), the waveforms FreeDATA uses on HF.

libcodec2 is LGPL-2.1 (David Rowe et al.) and is loaded dynamically; nothing of it is copied here. Each modem frame
carries ``payload_bytes`` of data plus a CRC16 added here (the library only reports a frame when its sync and LDPC
decode succeed; the CRC says whether the data is right). Samples are 16-bit at 8 kHz.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from dataclasses import dataclass

import numpy as np

MODES = {"datac0": 14, "datac1": 10, "datac3": 12, "datac4": 18, "datac13": 19, "datac14": 20}
FS = 8000


def _load() -> ctypes.CDLL:
    path = os.environ.get("HAM_MODEM_CODEC2_LIB") or ctypes.util.find_library("codec2")
    if not path:
        raise OSError("libcodec2 not found (install libcodec2)")
    lib = ctypes.CDLL(path)
    P, i, s = ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_short)
    b = ctypes.POINTER(ctypes.c_ubyte)
    for name, res, args in (
            ("freedv_open", P, [i]), ("freedv_close", None, [P]), ("freedv_nin", i, [P]),
            ("freedv_get_bits_per_modem_frame", i, [P]), ("freedv_get_n_max_modem_samples", i, [P]),
            ("freedv_get_n_tx_modem_samples", i, [P]), ("freedv_get_n_tx_preamble_modem_samples", i, [P]),
            ("freedv_get_n_tx_postamble_modem_samples", i, [P]), ("freedv_get_sync", i, [P]),
            ("freedv_rawdatatx", None, [P, s, b]), ("freedv_rawdatapreambletx", i, [P, s]),
            ("freedv_rawdatapostambletx", i, [P, s]), ("freedv_rawdatarx", i, [P, b, s]),
            ("freedv_gen_crc16", ctypes.c_ushort, [b, i]), ("freedv_set_frames_per_burst", None, [P, i])):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = res, args
    return lib


_lib: ctypes.CDLL | None = None


def lib() -> ctypes.CDLL:
    global _lib
    if _lib is None:
        _lib = _load()
    return _lib


@dataclass
class Frame:
    payload: bytes
    crc_ok: bool
    sync: bool


class Datac:
    """One datac mode: ``modulate(payloads)`` -> int16 samples (preamble, frames, postamble) and
    ``feed(samples)`` -> frames received so far."""

    def __init__(self, mode: str, frames_per_burst: int = 1) -> None:
        self.mode = mode
        self._lib = lib()
        self._f = self._lib.freedv_open(MODES[mode])
        if not self._f:
            raise OSError(f"this libcodec2 does not provide {mode}")
        self._lib.freedv_set_frames_per_burst(self._f, frames_per_burst)
        self.frame_bytes = self._lib.freedv_get_bits_per_modem_frame(self._f) // 8
        self.payload_bytes = self.frame_bytes - 2
        self._tx_n = self._lib.freedv_get_n_tx_modem_samples(self._f)
        self._pre_n = self._lib.freedv_get_n_tx_preamble_modem_samples(self._f)
        self._post_n = self._lib.freedv_get_n_tx_postamble_modem_samples(self._f)
        self._max_in = self._lib.freedv_get_n_max_modem_samples(self._f)
        self._buf = np.zeros(0, dtype=np.int16)

    def close(self) -> None:
        if self._f:
            self._lib.freedv_close(self._f)
            self._f = None

    __del__ = close

    # ------------------------------------------------------------------ transmit

    def modulate(self, payloads: list[bytes]) -> np.ndarray:
        out = []
        buf = (ctypes.c_short * max(self._pre_n, self._tx_n, self._post_n, 1))()
        n = self._lib.freedv_rawdatapreambletx(self._f, buf)
        out.append(np.frombuffer(buf, dtype=np.int16, count=n).copy())
        for p in payloads:
            if len(p) > self.payload_bytes:
                raise ValueError(f"{self.mode} carries {self.payload_bytes} bytes per frame")
            frame = bytearray(p.ljust(self.payload_bytes, b"\0"))
            crc = self._lib.freedv_gen_crc16((ctypes.c_ubyte * len(frame)).from_buffer(frame), len(frame))
            frame += bytes((crc >> 8, crc & 0xFF))
            self._lib.freedv_rawdatatx(self._f, buf, (ctypes.c_ubyte * len(frame)).from_buffer(frame))
            out.append(np.frombuffer(buf, dtype=np.int16, count=self._tx_n).copy())
        n = self._lib.freedv_rawdatapostambletx(self._f, buf)
        out.append(np.frombuffer(buf, dtype=np.int16, count=n).copy())
        return np.concatenate(out)

    # ------------------------------------------------------------------ receive

    def feed(self, samples: np.ndarray) -> list[Frame]:
        self._buf = np.concatenate((self._buf, np.asarray(samples, dtype=np.int16)))
        frames: list[Frame] = []
        out = (ctypes.c_ubyte * (self.frame_bytes + 8))()
        while True:
            nin = self._lib.freedv_nin(self._f)
            if len(self._buf) < nin:
                break
            chunk = (ctypes.c_short * nin).from_buffer_copy(self._buf[:nin].tobytes())
            self._buf = self._buf[nin:]
            n = self._lib.freedv_rawdatarx(self._f, out, chunk)
            if n > 0:
                data = bytes(out[:n])
                body, crc = data[:-2], int.from_bytes(data[-2:], "big")
                ok = n == self.frame_bytes and self._lib.freedv_gen_crc16(
                    (ctypes.c_ubyte * len(body)).from_buffer_copy(body), len(body)) == crc
                frames.append(Frame(body, ok, bool(self._lib.freedv_get_sync(self._f))))
        return frames
