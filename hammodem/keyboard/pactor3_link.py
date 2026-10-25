"""Working PACTOR-III link/terminal built on the existing physical layer."""

from __future__ import annotations

import numpy as np

from .pactor3 import (
    BURST_SAMPLES, CARRIER_FREQS, CS_REPLY_BITS, CS_SAMPLES, CYCLE_SAMPLES,
    DATA_BYTES, FS, Pactor3Demodulator, Pactor3MemArq, Pactor3Modulator,
    dpsk_demodulate_soft, M_PER_LEVEL, DATA_SYMS, CARRIER_INDICES,
)
from .pactor_link import PactorLinkBase
from .pactor_terminal import PactorTerminalBase


class Pactor3Link(PactorLinkBase):
    def __init__(self, mycall: str, fs: int = FS, level: int = 2, max_retries: int = 8,
                 on_text=None, on_event=None) -> None:
        self.mod = Pactor3Modulator(fs=fs, level=level)
        self.demod = Pactor3Demodulator(fs=fs, level=level)
        self._mem = Pactor3MemArq(level=level)
        super().__init__(
            mycall, fs=fs, level=level, data_bytes=DATA_BYTES[level],
            burst_len=BURST_SAMPLES, control_len=CS_SAMPLES,
            cycle_len=max(CYCLE_SAMPLES, BURST_SAMPLES + CS_SAMPLES + fs // 10),
            max_retries=max_retries, on_text=on_text, on_event=on_event,
        )

    def decode_data(self, audio: np.ndarray) -> dict | None:
        res = self.demod.feed_burst(audio)
        if self.state == "connected" and getattr(self, "role", None) == "irs":
            try:
                x = audio.astype(np.float64)
                m = M_PER_LEVEL[self.level]
                cidx = CARRIER_INDICES[self.level]
                softs = [dpsk_demodulate_soft(x, m, CARRIER_FREQS[ci], DATA_SYMS, self.fs) for ci in cidx]
                self._mem.add(softs)
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
        soft = dpsk_demodulate_soft(audio.astype(np.float64), 2, CARRIER_FREQS[5],
                                    CS_REPLY_BITS, self.fs)
        if len(soft) < CS_REPLY_BITS:
            return None
        value = 0
        for bit in (soft[:CS_REPLY_BITS] < 0).astype(np.uint8):
            value = (value << 1) | int(bit)
        return value


class Pactor3Terminal(PactorTerminalBase):
    LINK = Pactor3Link

    def make_link(self) -> Pactor3Link:
        return Pactor3Link(self.mycall, self.fs, self.level, on_text=self._rx_text, on_event=self._event)
