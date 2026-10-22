"""Raw PCM over UDP: signed 16-bit little-endian mono, one datagram per block.

This is the format Direwolf reads with ``ADEVICE udp:PORT`` (and our ``tools/audio_hub.py`` mixes), so a
Direwolf instance can monitor or join a test channel. Without a fixed ``peer`` the leg replies to whoever sent
to it last, which is how it joins the hub.
"""

from __future__ import annotations

import socket

import numpy as np


class UdpAudio:
    def __init__(self, listen: tuple[str, int], peer: tuple[str, int] | None, fs: int = 8000,
                 frame_ms: int | None = None) -> None:
        from .rtp import frame_ms as default_ms  # noqa: PLC0415
        frame_ms = frame_ms or default_ms(fs)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(listen)
        self.sock.setblocking(False)
        self.peer = peer
        self.frame_samples = fs * frame_ms // 1000
        self.name = f"udp://{listen[0]}:{self.sock.getsockname()[1]}" + (f" -> {peer[0]}:{peer[1]}" if peer else "")

    def fds(self):
        return [self.sock]

    def service(self, readable) -> list[np.ndarray]:
        blocks = []
        if self.sock not in readable:
            return blocks
        while True:
            try:
                data, addr = self.sock.recvfrom(8192)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:                    # ICMP port unreachable from a peer that is not up yet
                continue
            if self.peer is None:
                self.peer = addr
            if len(data) >= 2:
                blocks.append(np.frombuffer(data[:len(data) & ~1], "<i2"))
        return blocks

    def send(self, pcm: np.ndarray) -> None:
        if self.peer is None:
            return
        try:
            self.sock.sendto(pcm.astype("<i2").tobytes(), self.peer)
        except OSError:
            pass

    def close(self) -> None:
        self.sock.close()
