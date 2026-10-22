"""AX.25 over the codec2 OFDM data modes (datac0/1/3/4/13): the TNC's HF-capable burst modems.

A datac frame carries 14 to 510 payload bytes, far less than an AX.25 frame in the narrow modes, so AX.25 frames
are segmented across the chunks of a burst. This framing is ours (codec2 defines only the waveform and a CRC16 per
chunk); it is not FreeDATA's protocol. Chunk payload::

    byte 0   bit 7 = continuation of a frame that began in an earlier chunk, bits 6-0 = chunk index in the
             transmission (mod 128), so a missed chunk or burst is noticed
    byte 1.. records: u16 big-endian length (0 = padding follows) + AX.25 frame bytes

Whole frames that fit are packed into one chunk; a frame longer than a chunk starts a fresh chunk and continues in
the following ones. A chunk that fails its CRC loses only the frames it touches.

Needs libcodec2 (see ``hammodem.freedata.codec2``); audio is 8 kHz.
"""

from __future__ import annotations

import logging

import numpy as np

from ..freedata.codec2 import MODES, Datac

NAMES = tuple(m for m in MODES if m != "datac14")
log = logging.getLogger("hammodem.datac")
MAX_CHUNKS = 8                  # upper bound on chunks per burst (the header holds up to 16)
BURST_SECONDS = 12.0            # bursts are kept short: a timing slip in the VoIP path (a PBX re-timing its mix, a
                                # dropped packet) breaks the OFDM tracking for the rest of a burst, and each new
                                # burst's preamble re-acquires timing
QUIET_SAMPLES = 12000          # 1.5 s without a signal ends a transmission (repeats of a frame inside one are dropped)
BURST_GAP = 0.25                # seconds of silence between bursts of one transmission
MAX_FRAME = 2048


HEADER = 2


def segment(frames: list[bytes], capacity: int) -> list[bytes]:
    """Chunk payloads (each ``capacity`` bytes, header included) carrying ``frames``; the header's index and burst
    fields are filled in by ``number``."""
    room_max = capacity - HEADER
    chunks: list[bytes] = []
    cur = bytearray(HEADER)
    for f in frames:
        rec = len(f).to_bytes(2, "big") + f
        if len(cur) - HEADER + len(rec) <= room_max:
            cur += rec
            continue
        if len(cur) > HEADER:
            chunks.append(bytes(cur))
            cur = bytearray(HEADER)
        if len(rec) <= room_max:
            cur += rec
            continue
        for k in range(0, len(rec), room_max):          # longer than a chunk: spill over consecutive chunks
            chunks.append(bytes((0x80 if k else 0, 0)) + rec[k:k + room_max])
    if len(cur) > HEADER:
        chunks.append(bytes(cur))
    return [c.ljust(capacity, b"\0") for c in chunks]


def chunks_per_burst(frame_samples: int, fs: int = 8000) -> int:
    return max(1, min(MAX_CHUNKS, int(BURST_SECONDS * fs / frame_samples)))


def number(chunks: list[bytes], per_burst: int = MAX_CHUNKS) -> list[list[bytes]]:
    """Split into bursts of at most MAX_CHUNKS and fill the headers: byte 0 keeps the continuation bit and gets the
    chunk index (counted across the whole transmission, mod 128); byte 1 is (chunks in this burst - 1) << 4 | the
    chunk's position in it, so the receiver knows when a burst has ended."""
    bursts = []
    for b in range(0, len(chunks), per_burst):
        group = chunks[b:b + per_burst]
        bursts.append([bytes((c[0] & 0x80 | (b + k) & 0x7F, (len(group) - 1) << 4 | k)) + c[HEADER:]
                       for k, c in enumerate(group)])
    return bursts


class Reassembler:
    """Chunks (in burst order) back to AX.25 frames."""

    def __init__(self) -> None:
        self._pending = bytearray()
        self._need = 0
        self._next = -1

    def reset(self) -> None:
        self._pending.clear()
        self._need = 0
        self._next = -1

    def chunk(self, data: bytes) -> list[bytes]:
        out: list[bytes] = []
        cont, index = bool(data[0] & 0x80), data[0] & 0x7F
        body = data[HEADER:]
        if cont:
            if not self._need or index != self._next:
                self.reset()
                return out
            take = body[:self._need]
            self._pending += take
            self._need -= len(take)
            self._next = (index + 1) & 0x7F
            if not self._need:
                out.append(bytes(self._pending))
                self.reset()
            return out
        self.reset()
        pos = 0
        while pos + 2 <= len(body):
            n = int.from_bytes(body[pos:pos + 2], "big")
            if n == 0 or n > MAX_FRAME:
                break
            pos += 2
            if pos + n <= len(body):
                out.append(bytes(body[pos:pos + n]))
                pos += n
            else:
                self._pending = bytearray(body[pos:])
                self._need = n - len(self._pending)
                self._next = (index + 1) & 0x7F
                break
        return out


class DatacModulator:
    def __init__(self, mode: str, fs: int = 8000) -> None:
        if fs != 8000:
            raise ValueError("the datac modes run at 8 kHz")
        self.mode, self.fs = mode, fs
        self._probe = Datac(mode, 1)
        self.capacity = self._probe.payload_bytes
        self.per_burst = chunks_per_burst(self._probe.frame_samples, fs)
        self.fx25 = 0
        self.il2p = None

    def flags_for(self, seconds: float) -> int:
        return 0

    def modulate(self, frames: list[bytes], txdelay: float = 0.3, txtail: float = 0.03) -> np.ndarray:
        parts = [np.zeros(int(min(txdelay, 0.3) * self.fs), np.int16)]
        for burst in number(segment(frames, self.capacity), self.per_burst):    # numbered across bursts: a frame may straddle two
            parts.append(Datac(self.mode, len(burst)).modulate(burst))
            parts.append(np.zeros(int(BURST_GAP * self.fs), np.int16))
        parts.append(np.zeros(int(txtail * self.fs), np.int16))
        return np.concatenate(parts)


class DatacDemodulator:
    """codec2's burst receiver stays synchronised until it has seen as many chunks as it was told to expect, so the
    burst length (from each chunk's header) is used to drop sync at the end of a burst, and a deadline drops it when
    chunks stop coming -- otherwise a short burst would hide the next one."""

    def __init__(self, mode: str, fs: int = 8000) -> None:
        if fs != 8000:
            raise ValueError("the datac modes run at 8 kHz")
        self.mode, self.fs = mode, fs
        probe = Datac(mode, 1)
        self._rx = Datac(mode, chunks_per_burst(probe.frame_samples, fs))
        probe.close()
        self._asm = Reassembler()
        self.dcd = False
        self.bad_fcs = 0
        self.good_chunks = 0
        self._t = 0                      # samples fed
        self._deadline: int | None = None
        self._was_sync = False
        self._quiet = 0                  # samples since the receiver was last in sync
        self._epoch = 0                  # counts quiet periods: repeated frames within one epoch are one frame
        self._recent: dict[bytes, int] = {}

    def feed(self, pcm, erased: bool = False) -> list[bytes]:
        x = np.frombuffer(pcm, "<i2") if isinstance(pcm, (bytes, bytearray)) else np.asarray(pcm)
        frames: list[bytes] = []
        for i in range(0, len(x), 160):
            block = x[i:i + 160]
            chunks = self._rx.feed(block)
            self._quiet = 0 if self._rx.sync or chunks else self._quiet + len(block)
            if self._quiet == QUIET_SAMPLES:
                self._epoch += 1
                self._recent = {k: v for k, v in self._recent.items() if v == self._epoch}
            self._t += len(block)
            for ch in chunks:
                if not ch.crc_ok:
                    self.bad_fcs += 1
                    log.debug("%s: chunk failed its CRC", self.mode)
                    self._asm.reset()
                    continue
                self.good_chunks += 1
                total, pos = (ch.payload[1] >> 4) + 1, ch.payload[1] & 15
                log.debug("%s: chunk %d ok (%d of %d)", self.mode, ch.payload[0] & 0x7F, pos + 1, total)
                for f in self._asm.chunk(ch.payload):
                    if self._recent.get(f) != self._epoch:         # a repeat (--repeat) in the same transmission
                        frames.append(f)
                    self._recent[f] = self._epoch
                if pos + 1 >= total:
                    self._rx.unsync()
                    self._deadline = None
                else:
                    self._deadline = self._t + int((total - pos - 1 + 0.8) * self._rx.frame_samples)
            sync = self._rx.sync
            if sync and not self._was_sync:
                self._deadline = self._t + self._rx.preamble_samples + int(1.8 * self._rx.frame_samples)
            if sync and self._deadline is not None and self._t > self._deadline:
                log.debug("%s: burst ended without its last chunk", self.mode)
                self._rx.unsync()
                self._deadline = None
                self._asm.reset()
            self._was_sync = self._rx.sync
        self.dcd = self._rx.sync
        return frames
