"""Shared-channel access: p-persistent CSMA, TXDELAY/TXTAIL, frame bursts.

Time is the audio sample clock, not the wall clock: ``tx_samples(n, dcd)`` is called once per outgoing audio block
(20 ms on an RTP leg) and returns exactly ``n`` samples, the modulated burst while "keyed up" and digital silence
otherwise. Sending silence rather than nothing keeps RTP and conference bridges from treating us as gone.

Parameters follow KISS: TXDELAY, SLOTTIME and TXTAIL in seconds here (10 ms units on the wire), PERSIST 0-255
(transmit on a free slot with probability (P+1)/256), FULLDUPLEX skips carrier sense.
"""

from __future__ import annotations

import random
from collections import deque

import numpy as np


class Channel:
    def __init__(self, modulator, fs: int, *, txdelay: float = 0.3, persist: int = 63, slottime: float = 0.1,
                 txtail: float = 0.03, fullduplex: bool = False, maxframe: int = 7, max_queue: int = 64,
                 rng: random.Random | None = None) -> None:
        self.mod, self.fs = modulator, fs
        self.txdelay, self.persist, self.slottime = txdelay, persist, slottime
        self.txtail, self.fullduplex, self.maxframe = txtail, fullduplex, maxframe
        self.queue: deque[bytes] = deque(maxlen=max_queue)
        self.rng = rng or random.Random()
        self._burst = np.zeros(0, np.int16)
        self._clock = 0                         # samples elapsed
        self._next_slot = 0
        self.bursts = 0
        self.frames_sent = 0
        self.deferrals = 0                      # slots skipped because the channel was busy

    @property
    def transmitting(self) -> bool:
        return len(self._burst) > 0

    def send(self, frame: bytes) -> None:
        self.queue.append(frame)

    def tx_samples(self, n: int, dcd: bool) -> np.ndarray:
        if not self.transmitting and self.queue and self._clock >= self._next_slot:
            self._next_slot = self._clock + max(1, int(self.slottime * self.fs))
            if not self.fullduplex and dcd:
                self.deferrals += 1
            elif self.fullduplex or self.rng.random() < (self.persist + 1) / 256:
                frames = [self.queue.popleft() for _ in range(min(self.maxframe, len(self.queue)))]
                self._burst = self.mod.modulate(frames, self.txdelay, self.txtail)
                self.bursts += 1
                self.frames_sent += len(frames)
        self._clock += n
        out = self._burst[:n]
        self._burst = self._burst[n:]
        if len(out) < n:
            out = np.concatenate((out, np.zeros(n - len(out), np.int16)))
        return out
