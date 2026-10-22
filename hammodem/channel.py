"""Shared-channel access: p-persistent CSMA, TXDELAY/TXTAIL, frame bursts.

Time is the audio sample clock, not the wall clock: ``tx_samples(n, dcd)`` is called once per outgoing audio block
(20 ms on an RTP leg) and returns exactly ``n`` samples, the modulated burst while "keyed up" and digital silence
otherwise. Sending silence rather than nothing keeps RTP and conference bridges from treating us as gone.

Parameters follow KISS: TXDELAY, SLOTTIME and TXTAIL in seconds here (10 ms units on the wire), PERSIST 0-255
(transmit on a free slot with probability (P+1)/256), FULLDUPLEX skips carrier sense.

``collision_detect`` (CSMA/CD) is for conference bridges only: a bridge never returns a station's own audio, so
carrier heard *while we transmit* is another station keyed up on top of us. The burst is cut off, its frames go
back to the head of the queue, and the next attempt waits a random 1..2^k slots (k = attempts, capped), dropping
the frames after ``max_attempts``. Leave it off on echo legs (we would hear ourselves) and two-party calls (a
point-to-point call is full duplex: overlapping is harmless there).
"""

from __future__ import annotations

import random
from collections import deque

import numpy as np


class Channel:
    def __init__(self, modulator, fs: int, *, txdelay: float = 0.3, persist: int = 63, slottime: float = 0.1,
                 txtail: float = 0.03, fullduplex: bool = False, maxframe: int = 7, max_queue: int = 64,
                 collision_detect: bool = False, max_attempts: int = 8, rng: random.Random | None = None) -> None:
        self.mod, self.fs = modulator, fs
        self.txdelay, self.persist, self.slottime = txdelay, persist, slottime
        self.txtail, self.fullduplex, self.maxframe = txtail, fullduplex, maxframe
        self.queue: deque[bytes] = deque(maxlen=max_queue)
        self.collision_detect, self.max_attempts = collision_detect, max_attempts
        self.rng = rng or random.Random()
        self._burst_frames: list[bytes] = []
        self._attempts = 0
        self.collisions = 0
        self.dropped = 0
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
        if self.transmitting and self.collision_detect and dcd:
            self._collided()
        if not self.transmitting and self.queue and self._clock >= self._next_slot:
            self._next_slot = self._clock + max(1, int(self.slottime * self.fs))
            if not self.fullduplex and dcd:
                self.deferrals += 1
            elif self.fullduplex or self.rng.random() < (self.persist + 1) / 256:
                frames = [self.queue.popleft() for _ in range(min(self.maxframe, len(self.queue)))]
                self._burst = self.mod.modulate(frames, self.txdelay, self.txtail)
                self._burst_frames = frames
                self.bursts += 1
                self.frames_sent += len(frames)
        self._clock += n
        out = self._burst[:n]
        self._burst = self._burst[n:]
        if not self.transmitting and self._burst_frames:
            self._burst_frames, self._attempts = [], 0        # burst went out whole
        if len(out) < n:
            out = np.concatenate((out, np.zeros(n - len(out), np.int16)))
        return out

    def _collided(self) -> None:
        self.collisions += 1
        self._burst = np.zeros(0, np.int16)
        frames, self._burst_frames = self._burst_frames, []
        self.frames_sent -= len(frames)
        self._attempts += 1
        if self._attempts >= self.max_attempts:
            self.dropped += len(frames)
            self._attempts = 0
            return
        self.queue.extendleft(reversed(frames))
        slots = self.rng.randint(1, 2 ** min(self._attempts, 5))
        self._next_slot = self._clock + slots * max(1, int(self.slottime * self.fs))
