"""The TNC core: one modem + channel, and the DTE endpoints (KISS clients) attached to it.

I/O-free: the runtime hands it received audio (``audio_in``), asks it for audio to send (``audio_out``) and
passes bytes from each serial/TCP client to its endpoint. Everything heard on the channel goes to every
KISS endpoint, like a hardware TNC in KISS mode with several host programs sharing it.
"""

from __future__ import annotations

import logging
import time

import numpy as np

from . import kiss
from .ax25 import Ax25Error, Frame
from .channel import Channel
from . import modems

log = logging.getLogger("hammodem.tnc")


class KissEndpoint:
    """One host program speaking KISS over some byte link (pty, tty, TCP connection)."""

    def __init__(self, tnc: "Tnc", write, name: str = "") -> None:
        self.tnc, self._write, self.name = tnc, write, name
        self._dec = kiss.Decoder()

    def feed(self, data: bytes) -> None:
        for port, command, payload in self._dec.feed(data):
            self.tnc.kiss_command(self, port, command, payload)

    def deliver(self, frame: bytes, port: int = 0) -> None:
        self._write(kiss.encode(frame, port))

    def reset(self) -> None:
        self._dec = kiss.Decoder()


class Tnc:
    def __init__(self, modem: str = "afsk1200", fs: int = 8000, clock=time.monotonic, fx25: int = 0,
                 il2p: bool | None = None, silence_erasure_dbfs: float = -65.0,
                 silence_erasure_hold_ms: float = 80.0, **channel_args) -> None:
        mod, self.demod = modems.make(modem, fs)
        mod.fx25 = fx25                          # FX.25 check bytes on transmit; receive always accepts FX.25
        if il2p is not None:
            if modem.startswith("pam"):
                raise ValueError("IL2P is defined for AFSK and G3RUH modems, not PAM")
            mod.il2p = il2p                      # IL2P on transmit; AFSK/G3RUH receive always accept IL2P
        self.modem = modem
        self.fs = fs
        self.clock = clock                       # protocol timers (AX.25, beacons); tests pass simulated time
        self.silence_erasure_dbfs = silence_erasure_dbfs
        self._silence_hold_samples = max(0, int(fs * silence_erasure_hold_ms / 1000))
        self._rx_samples = 0
        self._rx_active_until = 0
        self.silence_erasure_blocks = 0
        self.channel = Channel(mod, fs, **channel_args)
        self.endpoints: list = []                # KissEndpoint / Tnc2Terminal: deliver(frame), feed(bytes)
        self.monitor = None                     # callable(direction, frame_bytes) for logging/TUI
        self.heard = 0

    def attach(self, write, name: str = "") -> KissEndpoint:
        ep = KissEndpoint(self, write, name)
        self.endpoints.append(ep)
        return ep

    def attach_tnc2(self, write, name: str = "", mycall: str = "NOCALL"):
        from .tnc2 import Tnc2Terminal  # noqa: PLC0415 -- tnc2 imports this module's peers
        term = Tnc2Terminal(self, write, name, mycall)
        self.endpoints.append(term)
        return term

    def tick(self) -> None:
        """Run per-port timers (AX.25 T1/T2/T3, beacons); called once per audio block."""
        for ep in self.endpoints:
            tick = getattr(ep, "tick", None)
            if tick is not None:
                tick()

    def detach(self, ep) -> None:
        if ep in self.endpoints:
            self.endpoints.remove(ep)

    @property
    def dcd(self) -> bool:
        return self.demod.dcd

    def audio_in(self, pcm: np.ndarray | bytes, erased: bool = False) -> list[bytes]:
        samples = np.frombuffer(pcm, dtype="<i2") if isinstance(pcm, (bytes, bytearray, memoryview)) else np.asarray(pcm)
        if self.silence_erasure_dbfs is not None and samples.size:
            x = samples.astype(np.float64) / 32768.0
            level_dbfs = 10 * np.log10(np.mean(x * x) + 1e-12)
            if level_dbfs > self.silence_erasure_dbfs:
                self._rx_active_until = self._rx_samples + len(samples) + self._silence_hold_samples
            elif self._rx_samples < self._rx_active_until:
                # Uplink RTP loss is mixed as ordinary silence by the bridge, so no RTP timestamp gap
                # reaches this receiver. During a recently active burst, treat near-silence as erased audio.
                erased = True
                self.silence_erasure_blocks += 1
        frames = self.demod.feed(pcm, erased)
        self._rx_samples += len(samples)
        for f in frames:
            self.heard += 1
            self._monitor("rx", f)
            for ep in self.endpoints:
                ep.deliver(f)
        return frames

    def audio_out(self, n: int) -> np.ndarray:
        return self.channel.tx_samples(n, self.dcd)

    def send(self, frame: bytes) -> None:
        self._monitor("tx", frame)
        self.channel.send(frame)

    def kiss_command(self, ep: KissEndpoint, port: int, command: int, payload: bytes) -> None:
        ch = self.channel
        if command == kiss.DATA:
            if port == 0 and len(payload) >= 15:
                self.send(payload)
            return
        if command == kiss.RETURN or not payload:
            return
        value = payload[0]
        if command == kiss.TXDELAY:
            ch.txdelay = value / 100
        elif command == kiss.PERSIST:
            ch.persist = value
        elif command == kiss.SLOTTIME:
            ch.slottime = value / 100
        elif command == kiss.TXTAIL:
            ch.txtail = value / 100
        elif command == kiss.FULLDUPLEX:
            ch.fullduplex = bool(value)
        log.debug("%s: KISS command %d = %d", ep.name, command, value)

    def _monitor(self, direction: str, raw: bytes) -> None:
        if self.monitor is None:
            return
        try:
            text = Frame.decode(raw).monitor()
        except Ax25Error as exc:
            text = f"<undecodable {len(raw)} bytes: {exc}>"
        self.monitor(direction, text)
