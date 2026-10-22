"""ham-modem command line: one TNC, its DTE ports and its audio leg, in one select() loop.

    ham-modem --kiss-pty ~/.ham-modem0 --kiss-tcp 8001 --sip-dial 7300@pbx.example:5060 -m
    ham-modem --kiss-pty ~/.ham-modem0 --udp-audio 7401:127.0.0.1:7300       # tools/audio_hub.py test channel

The audio clock is the outgoing block tick (20 ms): every tick the TNC produces one block (burst or silence).
"""

from __future__ import annotations

import argparse
import gc
import logging
import os
import select
import signal
import socket
import time

from .agw import AgwServer
from .kiss_tcp import KissTcpServer
from .modems import MODEMS
from .serial import DeviceSerial, PtySerial
from .tnc import Tnc
from .audio.rtp import CODECS
from .audio.sip import SipLeg
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
    def __init__(self, tnc: Tnc, audio, serials=(), servers=(), tap: tuple[str, int] | None = None) -> None:
        self.tnc, self.audio = tnc, audio
        self.tap = tap
        self._tap_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) if tap else None
        self.links = []
        for s in serials:
            mode, mycall = getattr(s, "tnc_mode", ("kiss", None))
            ep = tnc.attach_tnc2(s.write, s.name, mycall or "NOCALL") if mode == "tnc2" else tnc.attach(s.write, s.name)
            self.links.append((s, ep))
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
                if self._tap_sock is not None:
                    try:
                        self._tap_sock.sendto(block.astype("<i2").tobytes(), self.tap)
                    except OSError:
                        pass
            now = time.monotonic()
            if now - next_tx > 0.2:               # stalled (suspend, debugger): resync instead of bursting
                log.warning("audio clock slipped %.0f ms; resyncing", (now - next_tx) * 1000)
                next_tx = now
            while now >= next_tx:
                self.tnc.tick()
                self.audio.send(self.tnc.audio_out(n))
                next_tx += tick
            if self.tnc.dcd != self._dcd:
                self._dcd = self.tnc.dcd
                for s, _ in self.links:
                    s.update_lines(self._dcd)

    def close(self) -> None:
        rtp = getattr(self.audio, "rtp", None)
        ch = self.tnc.channel
        log.info("stats: heard %d frames (bad FCS %d), sent %d frames in %d bursts, %d busy deferrals, "
                 "%d collisions (%d frames dropped)%s",
                 self.tnc.heard, self.tnc.demod.bad_fcs, ch.frames_sent, ch.bursts, ch.deferrals, ch.collisions,
                 ch.dropped,
                 f"; rtp {rtp.stats}" if rtp is not None else "")
        for s, _ in self.links:
            s.close()
        for srv in self.servers:
            srv.close()
        self.audio.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ham-modem", description="AX.25 packet TNC over VoIP audio")
    ap.add_argument("--modem", choices=MODEMS, default="afsk1200")
    ap.add_argument("--rate", type=int, help="audio sample rate (default 8000; 16000 for g3ruh9600)")
    ap.add_argument("--kiss-pty", action="append", default=[], metavar="PATH",
                    help="create a pty speaking KISS, symlinked at PATH (repeatable)")
    ap.add_argument("--serial", action="append", default=[], metavar="DEV[:BAUD]",
                    help="speak KISS on a real serial port, e.g. /dev/ttyUSB0:9600 (repeatable)")
    ap.add_argument("--tnc2-pty", action="append", default=[], metavar="PATH",
                    help="create a pty with a TNC-2 command interpreter (cmd: prompt), symlinked at PATH")
    ap.add_argument("--tnc2-serial", action="append", default=[], metavar="DEV[:BAUD]",
                    help="TNC-2 command interpreter on a real serial port")
    ap.add_argument("--mycall", default="NOCALL", help="initial MYCALL for TNC-2 ports")
    ap.add_argument("--kiss-tcp", metavar="[HOST:]PORT", help="KISS-over-TCP server (Direwolf uses 8001)")
    ap.add_argument("--agw", metavar="[HOST:]PORT", help="AGWPE TCP API server (usually 8000)")
    leg = ap.add_mutually_exclusive_group(required=True)
    leg.add_argument("--udp-audio", metavar="LPORT[:HOST:PORT]",
                     help="raw s16le PCM over UDP: listen on LPORT, send to HOST:PORT (or to whoever sends first)")
    leg.add_argument("--sip-dial", metavar="EXT@HOST[:PORT]",
                     help="call a conference room or station and keep the call up (redial on hangup)")
    leg.add_argument("--sip-listen", action="store_true", help="answer incoming SIP calls (point-to-point link)")
    ap.add_argument("--sip-port", type=int, default=0, help="local SIP UDP port (default: any; 5060 for --sip-listen)")
    ap.add_argument("--media-port", type=int, default=0, help="local RTP port (default: any)")
    ap.add_argument("--sip-user", help="digest user / From user (password from HAM_MODEM_SIP_PASSWORD)")
    ap.add_argument("--local-ip", help="address to advertise in SIP/SDP (default: route towards the peer)")
    ap.add_argument("--codec", action="append", choices=CODECS, default=None,
                    help="codecs to offer, in order (default pcmu, pcma; l16-16k for wideband)")
    ap.add_argument("--txdelay", type=float, default=300, help="ms of flags before a frame (default 300)")
    ap.add_argument("--persist", type=int, default=63)
    ap.add_argument("--slottime", type=float, default=100, help="ms")
    ap.add_argument("--txtail", type=float, default=30, help="ms")
    ap.add_argument("--collision-detect", action="store_true",
                    help="CSMA/CD for conference rooms: abort and retry when another station keys up over us "
                         "(not for echo or two-party calls)")
    ap.add_argument("--audio-tap", metavar="HOST:PORT",
                    help="copy received audio as raw s16le UDP (e.g. to Direwolf: ADEVICE udp:PORT)")
    ap.add_argument("--sim-loss", type=float, default=0.0, help=argparse.SUPPRESS)   # lab: drop received RTP
    ap.add_argument("-m", "--monitor", action="store_true", help="print frames heard and sent")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    wide = args.modem == "g3ruh9600"
    if args.rate is None:
        args.rate = 16000 if wide else 8000
    if args.codec is None:
        args.codec = ["l16-16k"] if args.rate == 16000 else ["pcmu", "pcma"]
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")

    try:
        tnc = Tnc(args.modem, args.rate, txdelay=args.txdelay / 1000, persist=args.persist,
                  slottime=args.slottime / 1000, txtail=args.txtail / 1000, collision_detect=args.collision_detect)
    except ValueError as exc:
        ap.error(str(exc))
    if args.monitor:
        tnc.monitor = lambda d, text: print(f"{time.strftime('%H:%M:%S')} {d} {text}", flush=True)

    if args.udp_audio:
        lport, _, peer = args.udp_audio.partition(":")
        audio = UdpAudio(("0.0.0.0", int(lport)), _hostport(peer) if peer else None, args.rate)
    else:
        codecs = [CODECS[c] for c in args.codec]
        if any(c.rate != args.rate for c in codecs):
            ap.error(f"--rate {args.rate} does not match the codec rate(s) {sorted({c.rate for c in codecs})}")
        audio = SipLeg(dial=args.sip_dial, listen=args.sip_listen, codecs=codecs, local_ip=args.local_ip,
                       sip_port=args.sip_port or (5060 if args.sip_listen else 0), media_port=args.media_port,
                       user=args.sip_user, password=os.environ.get("HAM_MODEM_SIP_PASSWORD"),
                       sim_loss=args.sim_loss)
    serials = [_pty(p) for p in args.kiss_pty]
    for spec in args.serial:
        dev, _, baud = spec.partition(":")
        serials.append(DeviceSerial(dev, int(baud or 9600)))
    tnc2 = [_pty(p) for p in args.tnc2_pty]
    for spec in args.tnc2_serial:
        dev, _, baud = spec.partition(":")
        tnc2.append(DeviceSerial(dev, int(baud or 9600)))
    for s in tnc2:
        s.tnc_mode = ("tnc2", args.mycall)
    serials += tnc2
    servers = [KissTcpServer(tnc, *_hostport(args.kiss_tcp))] if args.kiss_tcp else []
    if args.agw:
        servers.append(AgwServer(tnc, *_hostport(args.agw), description=f"{args.modem} over VoIP"))

    rt = Runtime(tnc, audio, serials, servers, tap=_hostport(args.audio_tap) if args.audio_tap else None)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: setattr(rt, "running", False))
    log.info("%s at %d Hz; audio %s", args.modem, args.rate, audio.name)
    for s in serials:
        log.info("%s on %s%s", getattr(s, "tnc_mode", ("KISS",))[0].upper(), s.name,
                 f" (link {s._link})" if getattr(s, "_link", None) else "")
    for srv in servers:
        log.info("listening on %s", srv.name)
    gc.collect()
    gc.freeze()                                 # El-Modem: GC pauses starved the RTP sender; keep the heap small
    try:
        rt.run()
    finally:
        rt.close()


if __name__ == "__main__":
    main()
