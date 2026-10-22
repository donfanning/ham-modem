"""ham-modem command line: one TNC, its DTE ports and its audio leg, in one select() loop.

    ham-modem --kiss-pty ~/.ham-modem0 --kiss-tcp 8001 --udp-audio 0:127.0.0.1:7300 -m

The audio clock is the outgoing block tick (20 ms): every tick the TNC produces one block (burst or silence).
"""

from __future__ import annotations

import argparse
import gc
import logging
import os
import select
import signal
import time

from .kiss_tcp import KissTcpServer
from .modems.afsk import PROFILES
from .serial import DeviceSerial, PtySerial
from .tnc import Tnc
from .audio.udp import UdpAudio

log = logging.getLogger("hammodem")


def _hostport(text: str, default_host: str = "127.0.0.1") -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    return (host or default_host), int(port)


def _pty(link: str) -> PtySerial:
    link = os.path.expanduser(link)
    if os.path.islink(link):
        os.unlink(link)                       # stale link from a previous run
    return PtySerial(link)


class Runtime:
    def __init__(self, tnc: Tnc, audio, serials=(), servers=()) -> None:
        self.tnc, self.audio = tnc, audio
        self.links = [(s, tnc.attach(s.write, s.name)) for s in serials]
        self.servers = list(servers)
        self.running = True
        self._dcd = None

    def run(self) -> None:
        n = self.audio.frame_samples
        tick = n / self.tnc.fs
        next_tx = time.monotonic()
        while self.running:
            rfds = list(self.audio.fds())
            wfds = []
            for s, _ in self.links:
                rfds += s.fds()
                wfds += s.write_fds()
            for srv in self.servers:
                rfds += srv.fds()
                wfds += srv.write_fds()
            try:
                r, w, _ = select.select(rfds, wfds, [], max(0.0, next_tx - time.monotonic()))
            except InterruptedError:
                continue
            for s, ep in self.links:
                data, dropped = s.service(r, w)
                if dropped:
                    ep.reset()
                if data:
                    ep.feed(data)
            for srv in self.servers:
                srv.service(r, w)
            for block in self.audio.service(r):
                self.tnc.audio_in(block)
            now = time.monotonic()
            if now - next_tx > 0.2:               # stalled (suspend, debugger): resync instead of bursting
                log.warning("audio clock slipped %.0f ms; resyncing", (now - next_tx) * 1000)
                next_tx = now
            while now >= next_tx:
                self.audio.send(self.tnc.audio_out(n))
                next_tx += tick
            if self.tnc.dcd != self._dcd:
                self._dcd = self.tnc.dcd
                for s, _ in self.links:
                    s.update_lines(self._dcd)

    def close(self) -> None:
        for s, _ in self.links:
            s.close()
        for srv in self.servers:
            srv.close()
        self.audio.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ham-modem", description="AX.25 packet TNC over VoIP audio")
    ap.add_argument("--modem", choices=PROFILES, default="afsk1200")
    ap.add_argument("--rate", type=int, default=8000, help="audio sample rate (default 8000)")
    ap.add_argument("--kiss-pty", action="append", default=[], metavar="PATH",
                    help="create a pty speaking KISS, symlinked at PATH (repeatable)")
    ap.add_argument("--serial", action="append", default=[], metavar="DEV[:BAUD]",
                    help="speak KISS on a real serial port, e.g. /dev/ttyUSB0:9600 (repeatable)")
    ap.add_argument("--kiss-tcp", metavar="[HOST:]PORT", help="KISS-over-TCP server (Direwolf uses 8001)")
    ap.add_argument("--udp-audio", required=True, metavar="LPORT[:HOST:PORT]",
                    help="raw s16le PCM over UDP: listen on LPORT, send to HOST:PORT (or to whoever sends first)")
    ap.add_argument("--txdelay", type=float, default=300, help="ms of flags before a frame (default 300)")
    ap.add_argument("--persist", type=int, default=63)
    ap.add_argument("--slottime", type=float, default=100, help="ms")
    ap.add_argument("--txtail", type=float, default=30, help="ms")
    ap.add_argument("-m", "--monitor", action="store_true", help="print frames heard and sent")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")

    tnc = Tnc(args.modem, args.rate, txdelay=args.txdelay / 1000, persist=args.persist,
              slottime=args.slottime / 1000, txtail=args.txtail / 1000)
    if args.monitor:
        tnc.monitor = lambda d, text: print(f"{time.strftime('%H:%M:%S')} {d} {text}", flush=True)

    lport, _, peer = args.udp_audio.partition(":")
    audio = UdpAudio(("0.0.0.0", int(lport)), _hostport(peer) if peer else None, args.rate)
    serials = [_pty(p) for p in args.kiss_pty]
    for spec in args.serial:
        dev, _, baud = spec.partition(":")
        serials.append(DeviceSerial(dev, int(baud or 9600)))
    servers = [KissTcpServer(tnc, *_hostport(args.kiss_tcp))] if args.kiss_tcp else []

    rt = Runtime(tnc, audio, serials, servers)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: setattr(rt, "running", False))
    log.info("%s at %d Hz; audio %s", args.modem, args.rate, audio.name)
    for s in serials:
        log.info("KISS on %s%s", s.name, f" (link {s._link})" if getattr(s, "_link", None) else "")
    for srv in servers:
        log.info("KISS on %s", srv.name)
    gc.collect()
    gc.freeze()                                 # El-Modem: GC pauses starved the RTP sender; keep the heap small
    try:
        rt.run()
    finally:
        rt.close()


if __name__ == "__main__":
    main()
