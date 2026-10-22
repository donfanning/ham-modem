"""RTP audio (RFC 3550) for one call: G.711 or L16 payloads, 20 ms frames, timestamp-gap concealment.

Receive keeps the sample clock honest: a timestamp jump means lost packets, and the gap is filled so the
demodulator's bit clock stays aligned. As El-Modem measured, repeating the last frame keeps a carrier
phase-continuous through short gaps (here: a 20 ms frame is a whole number of 1200/2200 Hz cycles at 8 kHz); long
gaps get silence. Late or duplicate packets are dropped rather than rewinding the clock (no reordering buffer:
the conference bridge in front of us already de-jitters).

Only packets from the negotiated remote address are accepted, and the first SSRC heard is latched.
"""

from __future__ import annotations

import os
import socket
import struct
from dataclasses import dataclass

import numpy as np

from . import g711

CONCEAL_MAX_FRAMES = 5
MAX_GAP_SAMPLES = 8000


@dataclass(frozen=True)
class Codec:
    name: str              # SDP encoding name
    pt: int
    rate: int

    def encode(self, pcm: np.ndarray) -> bytes:
        if self.name == "PCMU":
            return g711.encode(pcm, "pcmu").astype(np.uint8).tobytes()
        if self.name == "PCMA":
            return g711.encode(pcm, "pcma").astype(np.uint8).tobytes()
        return pcm.astype(">i2").tobytes()                       # L16 is network byte order

    def decode(self, payload: bytes) -> np.ndarray:
        if self.name == "PCMU":
            return g711.decode(np.frombuffer(payload, np.uint8), "pcmu").astype(np.int16)
        if self.name == "PCMA":
            return g711.decode(np.frombuffer(payload, np.uint8), "pcma").astype(np.int16)
        return np.frombuffer(payload[:len(payload) & ~1], ">i2").astype(np.int16)


PCMU, PCMA = Codec("PCMU", 0, 8000), Codec("PCMA", 8, 8000)
L16_16K = Codec("L16", 96, 16000)
L16_48K = Codec("L16", 97, 48000)
CODECS = {"pcmu": PCMU, "pcma": PCMA, "l16-16k": L16_16K, "l16-48k": L16_48K}


def frame_ms(rate: int) -> int:
    """Packet duration: 20 ms, but 10 ms at 32 kHz and up so an L16 packet stays under a 1500-byte MTU
    (20 ms of L16/48000 would be 1920 bytes and fragment)."""
    return 10 if rate >= 32000 else 20


class RtpSession:
    def __init__(self, sock: socket.socket, codec: Codec, remote: tuple[str, int], frame_ms: int = 20) -> None:
        self.sock, self.codec, self.remote = sock, codec, remote
        self.frame_samples = codec.rate * frame_ms // 1000
        self._seq = int.from_bytes(os.urandom(2), "big")
        self._ts = int.from_bytes(os.urandom(4), "big")
        self._ssrc = int.from_bytes(os.urandom(4), "big")
        self._rx_ssrc: int | None = None
        self._rx_ts: int | None = None
        self._rx_len = self.frame_samples          # samples in the previous packet (peers choose their own ptime)
        self._last = np.zeros(self.frame_samples, np.int16)
        self.stats = {"sent": 0, "received": 0, "late_or_duplicate": 0, "concealed_samples": 0, "foreign": 0}

    def send(self, pcm: np.ndarray) -> None:
        header = struct.pack("!BBHII", 0x80, self.codec.pt, self._seq, self._ts, self._ssrc)
        try:
            self.sock.sendto(header + self.codec.encode(pcm), self.remote)
        except OSError:
            pass
        self._seq = (self._seq + 1) & 0xFFFF
        self._ts = (self._ts + len(pcm)) & 0xFFFFFFFF
        self.stats["sent"] += 1

    def receive(self, packet: bytes, addr) -> list[np.ndarray]:
        """Audio blocks (concealment first, then the packet's own) for one datagram; [] if rejected."""
        if addr[0] != self.remote[0] or len(packet) < 12 or packet[0] >> 6 != 2:
            self.stats["foreign"] += 1
            return []
        cc, pt = packet[0] & 0x0F, packet[1] & 0x7F
        if pt != self.codec.pt:
            return []                                             # comfort noise, telephone-event, ...
        ts, ssrc = struct.unpack_from("!II", packet, 4)
        start = 12 + 4 * cc
        if packet[0] & 0x10 and len(packet) >= start + 4:             # header extension
            start += 4 + 4 * struct.unpack_from("!H", packet, start + 2)[0]
        end = len(packet) - (packet[-1] if packet[0] & 0x20 else 0)  # padding
        if ssrc != self._rx_ssrc:
            self._rx_ssrc, self._rx_ts = ssrc, None                   # a new source restarts the clock
        out = []
        if self._rx_ts is not None:
            step = (ts - self._rx_ts) & 0xFFFFFFFF
            if step == 0 or step >= 0x80000000:
                self.stats["late_or_duplicate"] += 1
                return []
            missing = step - self._rx_len
            if 0 < missing <= MAX_GAP_SAMPLES:
                self.stats["concealed_samples"] += missing
                out.append(self._conceal(missing))
        self._rx_ts = ts
        pcm = self.codec.decode(packet[start:end])
        if len(pcm):
            self._last = pcm
            self._rx_len = len(pcm)
        self.stats["received"] += 1
        out.append(pcm)
        return out

    def _conceal(self, missing: int) -> np.ndarray:
        if missing > CONCEAL_MAX_FRAMES * self._rx_len:
            return np.zeros(missing, np.int16)
        return np.resize(self._last, missing)
