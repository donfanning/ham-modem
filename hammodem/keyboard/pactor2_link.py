"""Working PACTOR-II link/terminal built on the existing physical layer."""

from __future__ import annotations

import numpy as np

from .pactor2 import (
    BURST_SAMPLES, CARRIER_0, CS_REPLY_BITS, CS_SAMPLES, CYCLE_SAMPLES,
    DATA_BYTES, FS, Pactor2Demodulator, Pactor2MemArq, Pactor2Modulator,
    dpsk_demodulate_soft, BITS_PER_SYM, M_STATES, DATA_SYMS,
)
from .pactor_link import PactorLinkBase
from .pactor_terminal import PactorTerminalBase


class Pactor2Link(PactorLinkBase):
    def __init__(self, mycall: str, fs: int = FS, level: int = 2, max_retries: int = 8,
                 on_text=None, on_event=None) -> None:
        self.mod = Pactor2Modulator(fs=fs, level=level)
        self.demod = Pactor2Demodulator(fs=fs, level=level)
        self._mem = Pactor2MemArq(level=level)
        super().__init__(
            mycall, fs=fs, level=level, data_bytes=DATA_BYTES[level],
            burst_len=BURST_SAMPLES, control_len=CS_SAMPLES,
            cycle_len=max(CYCLE_SAMPLES, BURST_SAMPLES + CS_SAMPLES + fs // 10),
            max_retries=max_retries, on_text=on_text, on_event=on_event,
        )

    def decode_data(self, audio: np.ndarray) -> dict | None:
        res = self.demod.feed_burst(audio)
        # Memory ARQ combining when receiving data (IRS) across retries
        if self.state == "connected" and getattr(self, "role", None) == "irs":
            try:
                n_data = DATA_SYMS * BITS_PER_SYM[self.level]
                soft0 = dpsk_demodulate_soft(audio.astype(np.float64), M_STATES[self.level], self.mod.c0, DATA_SYMS, self.fs)
                soft1 = dpsk_demodulate_soft(audio.astype(np.float64), M_STATES[self.level], self.mod.c1, DATA_SYMS, self.fs)
                self._mem.add(soft0[:n_data], soft1[:n_data])
                combined = self._mem.try_decode()
                if combined is not None:
                    res = combined
            except Exception:
                pass
        else:
            self._mem.reset()
        if res is not None:
            self._mem.reset()
        return res

    def cs_decode(self, audio: np.ndarray) -> int | None:
        soft = dpsk_demodulate_soft(audio.astype(np.float64), 2, CARRIER_0,
                                    CS_REPLY_BITS, self.fs)
        if len(soft) < CS_REPLY_BITS:
            return None
        value = 0
        for bit in (soft[:CS_REPLY_BITS] < 0).astype(np.uint8):
            value = (value << 1) | int(bit)
        return value


class Pactor2Terminal(PactorTerminalBase):
    LINK = Pactor2Link

    def make_link(self) -> Pactor2Link:
        return Pactor2Link(self.mycall, self.fs, self.level, on_text=self._rx_text, on_event=self._event)
