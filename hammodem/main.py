"""ham-modem command line: one TNC, its DTE ports and its audio leg, in one select() loop.

    ham-modem --kiss-pty ~/.ham-modem0 --kiss-tcp 8001 --sip-dial 7300@pbx.example:5060 -m
    ham-modem --kiss-pty ~/.ham-modem0 --udp-audio 7401:127.0.0.1:7300       # tools/audio_hub.py test channel
    ham-modem --modem ardop --mycall K1ABC --ardop-host 8515 --sip-dial 7300@pbx.example   # ARDOP for Winlink

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

import numpy as np

from .agw import AgwServer
from .kiss_tcp import KissTcpServer
from .modems import MODEMS, default_rate
from .serial import DeviceSerial, PtySerial, TcpSerial
from .tnc import Tnc
from .audio.rtp import CODECS, ConcealedPcm
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
                self.tnc.audio_in(np.asarray(block), isinstance(block, ConcealedPcm))
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
        log.info("stats: heard %d frames (bad FCS %d, FX.25 repaired %d), sent %d frames in %d bursts, "
                 "%d busy deferrals, %d collisions (%d frames dropped)%s",
                 self.tnc.heard, self.tnc.demod.bad_fcs, getattr(self.tnc.demod, "fx25_corrected", 0),
                 ch.frames_sent, ch.bursts, ch.deferrals, ch.collisions, ch.dropped,
                 f"; rtp {rtp.stats}" if rtp is not None else "")
        for s, _ in self.links:
            s.close()
        for srv in self.servers:
            srv.close()
        self.audio.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ham-modem", description="AX.25 packet TNC over VoIP audio")
    ap.add_argument("--modem", choices=(*MODEMS, "ardop", "amtor", "pactor", "rtty", "psk31", "psk63", "mfsk16",
                                       "olivia", "contestia", "dominoex", "thor", "js8"), default="afsk1200",
                    help="'ardop': an ARDOP (Winlink) TNC with an ardopcf-compatible host interface instead of AX.25")
    ap.add_argument("--rate", type=int, help="audio sample rate (default: 8000 for AFSK, 16000 for g3ruh9600, "
                                             "48000 for g3ruh19200/38400)")
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
    leg.add_argument("--iax-dial", metavar="EXT@HOST[:PORT]",
                     help="IAX2: call EXT through the PBX and keep the call up (G.711 mu-law; AFSK modes)")
    leg.add_argument("--iax-listen", metavar="HOST[:PORT]", help="IAX2: register with the PBX and answer calls")
    ap.add_argument("--udp-bind", default="0.0.0.0", metavar="ADDR", help="local address for --udp-audio")
    ap.add_argument("--iax-user", default="station", help="IAX2 user (secret from HAM_MODEM_IAX_SECRET)")
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
    ap.add_argument("--fx25", type=int, choices=(0, 16, 32, 64), default=0,
                    help="send frames FX.25-wrapped with this many Reed-Solomon check bytes (receive always "
                         "accepts FX.25; plain AX.25 stations still decode FX.25 frames)")
    ap.add_argument("--il2p", choices=("off", "on", "max"), default="off",
                    help="transmit IL2P (Direwolf 1.7 / NinoTNC) instead of AX.25; 'max' = maximum FEC. "
                         "Receive always accepts IL2P on AFSK and G3RUH. Plain AX.25 stations cannot decode it")
    ap.add_argument("--repeat", type=int, default=1, metavar="N",
                    help="send every frame N times, spaced so one lost packet cannot hit all copies "
                         "(for the fast modes; receivers drop duplicates)")
    ap.add_argument("--collision-detect", action="store_true",
                    help="CSMA/CD for conference rooms: abort and retry when another station keys up over us "
                         "(not for echo or two-party calls)")
    ap.add_argument("--audio-tap", metavar="HOST:PORT",
                    help="copy received audio as raw s16le UDP (e.g. to Direwolf: ADEVICE udp:PORT)")
    ap.add_argument("--sim-loss", type=float, default=0.0, help=argparse.SUPPRESS)   # lab: drop received RTP
    ap.add_argument("--text-pty", action="append", default=[], metavar="PATH",
                    help="AMTOR: text terminal on a pty symlinked at PATH (type, read; /help for commands)")
    ap.add_argument("--text-tcp", metavar="[HOST:]PORT", help="AMTOR: the same text terminal over TCP (telnet/nc)")
    ap.add_argument("--rtty-baud", type=float, default=45.45, help="RTTY: baud rate (45.45, 50, 75, 100)")
    ap.add_argument("--rtty-shift", type=float, default=170.0, help="RTTY: shift in Hz (170, 425, 850)")
    ap.add_argument("--kb-center", type=float, default=1700.0, help="keyboard modes: audio centre frequency")
    ap.add_argument("--rtty-reverse", action="store_true", help="RTTY: mark is the lower tone")
    ap.add_argument("--amtor-reply", type=int, default=50, metavar="MS",
                    help="AMTOR ARQ: slave reply delay after a received burst (the audio round trip must fit "
                         "in 240 ms minus this minus 70 ms)")
    ap.add_argument("--ardop-host", metavar="[HOST:]PORT", default="127.0.0.1:8515",
                    help="ARDOP: host interface command port (data on PORT+1), as ardopcf (default 8515)")
    ap.add_argument("--arqbw", default="500MAX", help="ARDOP: ARQ bandwidth (e.g. 500MAX, 2000MAX, 200FORCED)")
    ap.add_argument("--drive", type=int, default=100, help="ARDOP: transmit level in percent (DRIVELEVEL)")
    ap.add_argument("-m", "--monitor", action="store_true", help="print frames heard and sent")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    if args.rate is None:
        args.rate = default_rate(args.modem)
    if args.codec is None:
        args.codec = {16000: ["l16-16k"], 48000: ["l16-48k"]}.get(args.rate, ["pcmu", "pcma"])
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")

    if args.modem in ("ardop", "amtor", "pactor", "rtty"):
        tnc = None
    else:
        try:
            tnc = Tnc(args.modem, args.rate, txdelay=args.txdelay / 1000, persist=args.persist,
                      slottime=args.slottime / 1000, txtail=args.txtail / 1000,
                      collision_detect=args.collision_detect, fx25=args.fx25, repeat=args.repeat,
                      il2p={"off": None, "on": False, "max": True}[args.il2p])
        except ValueError as exc:
            ap.error(str(exc))
    if args.monitor and tnc is not None:
        tnc.monitor = lambda d, text: print(f"{time.strftime('%H:%M:%S')} {d} {text}", flush=True)

    if args.iax_dial or args.iax_listen:
        if args.rate != 8000:
            ap.error("IAX2 legs carry G.711 mu-law (8 kHz): use an AFSK modem or a SIP L16 leg")
        from .audio.iax import IaxLeg  # noqa: PLC0415
        ext, _, server = (args.iax_dial or "").rpartition("@")
        audio = IaxLeg(server if args.iax_dial else args.iax_listen, args.iax_user,
                       os.environ.get("HAM_MODEM_IAX_SECRET", ""), dial=ext or None, listen=bool(args.iax_listen))
    elif args.udp_audio:
        lport, _, peer = args.udp_audio.partition(":")
        audio = UdpAudio((args.udp_bind, int(lport)), _hostport(peer) if peer else None, args.rate)
    else:
        codecs = [CODECS[c] for c in args.codec]
        if any(c.rate != args.rate for c in codecs):
            ap.error(f"--rate {args.rate} does not match the codec rate(s) {sorted({c.rate for c in codecs})}")
        audio = SipLeg(dial=args.sip_dial, listen=args.sip_listen, codecs=codecs, local_ip=args.local_ip,
                       sip_port=args.sip_port or (5060 if args.sip_listen else 0), media_port=args.media_port,
                       user=args.sip_user, password=os.environ.get("HAM_MODEM_SIP_PASSWORD"),
                       sim_loss=args.sim_loss)
    if args.modem == "ardop":
        from .ardop.arq import ArqStation  # noqa: PLC0415
        from .ardop.tnc import run as run_ardop  # noqa: PLC0415
        station = ArqStation(args.mycall, fs=args.rate, bandwidth=args.arqbw.upper(), drive=args.drive / 100)
        run_ardop(station, audio, *_hostport(args.ardop_host))
        return
    if args.modem in ("amtor", "pactor", "rtty", "psk31", "psk63", "mfsk16", "olivia", "contestia",
                      "dominoex", "thor", "js8"):
        ports = [_pty(p) for p in args.text_pty]
        if args.text_tcp:
            ports.append(TcpSerial(*_hostport(args.text_tcp)))
        if not ports:
            ap.error(f"--modem {args.modem} needs --text-pty or --text-tcp")
    if args.modem == "rtty":
        from .keyboard.rtty import RttyDemodulator, RttyModulator  # noqa: PLC0415
        from .keyboard.tnc import KeyboardTnc, run as run_kb  # noqa: PLC0415
        kw = dict(fs=args.rate, baud=args.rtty_baud, shift=args.rtty_shift, center=args.kb_center,
                  reverse=args.rtty_reverse)
        log.info("RTTY %.2f baud, %g Hz shift at %d Hz; audio %s; text on %s", args.rtty_baud, args.rtty_shift,
                 args.rate, audio.name, ", ".join(p.name for p in ports))
        run_kb(KeyboardTnc("rtty", RttyModulator(**kw), RttyDemodulator(**kw), args.rate), audio, ports)
        return
    if args.modem in ("psk31", "psk63"):
        from .keyboard.psk31 import PskDemodulator, PskModulator  # noqa: PLC0415
        from .keyboard.tnc import KeyboardTnc, run as run_kb  # noqa: PLC0415
        baud = 31.25 if args.modem == "psk31" else 62.5
        kw = dict(fs=args.rate, baud=baud, center=args.kb_center)
        log.info("%s at %.2f baud, %d Hz centre; audio %s; text on %s", args.modem.upper(), baud,
                 args.kb_center, audio.name, ", ".join(p.name for p in ports))
        run_kb(KeyboardTnc(args.modem, PskModulator(**kw), PskDemodulator(**kw), args.rate), audio, ports)
        return
    if args.modem == "mfsk16":
        from .keyboard.mfsk16 import MfskDemodulator, MfskModulator  # noqa: PLC0415
        from .keyboard.tnc import KeyboardTnc, run as run_kb  # noqa: PLC0415
        kw = dict(fs=args.rate, center=args.kb_center)
        log.info("MFSK16 at 15.625 baud, %d Hz centre; audio %s; text on %s",
                 args.kb_center, audio.name, ", ".join(p.name for p in ports))
        run_kb(KeyboardTnc("mfsk16", MfskModulator(**kw), MfskDemodulator(**kw), args.rate,
                           lead=5.0, tail=5.0), audio, ports)
        return
    if args.modem in ("olivia", "contestia"):
        from .keyboard.olivia import OliviaDemodulator, OliviaModulator  # noqa: PLC0415
        from .keyboard.tnc import KeyboardTnc, run as run_kb  # noqa: PLC0415
        is_contestia = args.modem == "contestia"
        kw = dict(fs=args.rate, center=args.kb_center, contestia=is_contestia)
        log.info("%s 32/1000 at %.2f baud, %d Hz centre; audio %s; text on %s",
                 args.modem.upper(), OliviaModulator(**kw).baud,
                 args.kb_center, audio.name, ", ".join(p.name for p in ports))
        run_kb(KeyboardTnc(args.modem, OliviaModulator(**kw), OliviaDemodulator(**kw), args.rate,
                           lead=4.0, tail=4.0), audio, ports)
        return
    if args.modem in ("dominoex", "thor"):
        from .keyboard.dominoex import DominoDemodulator, DominoModulator  # noqa: PLC0415
        from .keyboard.tnc import KeyboardTnc, run as run_kb  # noqa: PLC0415
        is_thor = args.modem == "thor"
        kw = dict(fs=args.rate, symlen=1024, doublespaced=2, center=args.kb_center, thor=is_thor, idepth=10)
        baud = args.rate / 1024
        mode_name = "Thor 8" if is_thor else "DominoEX 8"
        log.info("%s at %.2f baud, %d Hz centre; audio %s; text on %s",
                 mode_name, baud, args.kb_center, audio.name, ", ".join(p.name for p in ports))
        run_kb(KeyboardTnc(args.modem, DominoModulator(**kw), DominoDemodulator(**kw), args.rate,
                           lead=4.0, tail=4.0), audio, ports)
        return
    if args.modem == "js8":
        from .keyboard.js8 import Js8Demodulator, Js8Modulator  # noqa: PLC0415
        from .keyboard.tnc import KeyboardTnc, run as run_kb  # noqa: PLC0415
        symlen = 1280
        baud = args.rate / symlen
        kw = dict(fs=args.rate, symlen=symlen, center=args.kb_center)
        log.info("JS8 Normal at %.2f baud, %d Hz centre; audio %s; text on %s",
                 baud, args.kb_center, audio.name, ", ".join(p.name for p in ports))
        run_kb(KeyboardTnc("js8", Js8Modulator(**kw), Js8Demodulator(**kw), args.rate,
                           lead=2.0, tail=2.0), audio, ports)
        return
    if args.modem == "pactor":
        from .pactor.tnc import PactorTnc, run as run_pactor  # noqa: PLC0415
        log.info("PACTOR-I %s at %d Hz; audio %s; text on %s", args.mycall, args.rate, audio.name,
                 ", ".join(p.name for p in ports))
        run_pactor(PactorTnc(args.mycall, args.rate, center=args.kb_center), audio, ports)
        return
    if args.modem == "amtor":
        from .amtor.tnc import AmtorTnc, run as run_amtor  # noqa: PLC0415
        log.info("AMTOR %s (selcal %s) at %d Hz; audio %s; text on %s", args.mycall, "", args.rate, audio.name,
                 ", ".join(p.name for p in ports))
        run_amtor(AmtorTnc(args.mycall, args.rate, center=args.kb_center, reply_ms=args.amtor_reply), audio, ports)
        return
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
