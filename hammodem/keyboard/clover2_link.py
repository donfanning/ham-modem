"""Working Clover-II link/terminal built on the existing physical layer."""

from __future__ import annotations

import numpy as np

from .clover2 import Clover2Tnc, CloverFrame, Mode
from .pactor_link import LinkBase, pack_status
from .pactor_terminal import PactorTerminalBase

# A one-byte control frame is enough for the link layer.  The published CCB
# has more fields; this is the local ham-modem ARQ protocol, not HAL framing.
_CTRL_KIND = 0x07
_CTRL_SEQ = 0x08
_CTRL_ARG_SHIFT = 4
_CTRL_ARG = 0x0F


def _pack_ctrl(kind: int, seq: int = 0, arg: int = 0) -> int:
    return (kind & _CTRL_KIND) | ((seq & 1) << 3) | ((arg & _CTRL_ARG) << _CTRL_ARG_SHIFT)


def _unpack_ctrl(value: int) -> tuple[int, int, int]:
    return (value & _CTRL_KIND, (value >> 3) & 1, (value >> _CTRL_ARG_SHIFT) & _CTRL_ARG)


class Clover2Link(LinkBase):
    def __init__(self, mycall: str, fs: int = 8000, level: int = 2, max_retries: int = 8,
                 mode: Mode = Mode.QPSM, bias: str = "normal",
                 on_text=None, on_event=None) -> None:
        self.tnc = Clover2Tnc(fs=fs, mode=mode, bias=bias)
        self.mode = mode
        self.bias = bias
        data_bytes = CloverFrame._DATA_BYTES[bias] - 1
        # Every Clover ECB is 255 bytes, so all frames have the same duration.
        burst_len = len(self.tnc.transmit(b"X"))
        super().__init__(
            mycall, fs=fs, level=level, data_bytes=data_bytes,
            burst_len=burst_len, control_len=burst_len,
            cycle_len=2 * burst_len + fs // 2,
            max_retries=max_retries, on_text=on_text, on_event=on_event,
        )

    def make_data(self, payload: bytes, status: int) -> np.ndarray:
        return self.tnc.transmit(bytes([status]) + payload)

    def decode_data(self, audio: np.ndarray) -> dict | None:
        self.tnc.reset()
        raw = self.tnc.feed(audio)
        if not raw:
            return None
        return {"status": raw[0], "data": raw[1:]}

    def make_control(self, kind: int, seq: int = 0, arg: int = 0) -> np.ndarray:
        return self.tnc.transmit(bytes([_pack_ctrl(kind, seq, arg)]))

    def decode_control(self, audio: np.ndarray) -> tuple[int, int, int] | None:
        self.tnc.reset()
        raw = self.tnc.feed(audio)
        if not raw:
            return None
        return _unpack_ctrl(raw[0])


class Clover2Terminal(PactorTerminalBase):
    LINK = Clover2Link

    def __init__(self, mycall: str, fs: int = 8000, level: int = 2, write=None,
                 max_retries: int = 8, mode: Mode = Mode.QPSM, bias: str = "normal") -> None:
        self.mode = mode
        self.bias = bias
        super().__init__(mycall, fs=fs, level=level, write=write, max_retries=max_retries)

    def make_link(self) -> Clover2Link:
        return Clover2Link(
            self.mycall, self.fs, self.level, mode=self.mode, bias=self.bias,
            on_text=self._rx_text, on_event=self._event,
        )
