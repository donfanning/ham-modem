#!/usr/bin/env python3
"""Milestone-1 harness: N ham-modem stations in an Asterisk ConfBridge room exchanging UI frames over KISS-TCP.

    lab/confbridge_test.py [--stations 3] [--frames 10] [--loss 0.0] [--room 7300@172.31.73.10]
                           [--tap 127.0.0.1:7355]    # station 0's received audio to a Direwolf monitor

Every station sends ``--frames`` frames at random times; every other station should hear each one. Prints the
delivery matrix and the overall rate. Needs the lab PBX (``cd lab/asterisk && docker compose up -d``).
"""

import argparse
import os
import random
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hammodem import kiss  # noqa: E402
from hammodem.ax25 import Frame  # noqa: E402


def tc(*argv, capture=False):
    cmd = ["tc", *argv]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    result = subprocess.run(cmd, check=True, text=True, capture_output=capture)
    return result.stdout if capture else ""


def enable_netem(loss_pct, pbx_ip, media_port, requested_device=None):
    route = subprocess.check_output(["ip", "route", "get", pbx_ip], text=True).split()
    device = requested_device or route[route.index("dev") + 1]
    existing = tc("qdisc", "show", "dev", device, capture=True)
    if not existing.strip().startswith("qdisc noqueue 0: root"):
        raise RuntimeError(f"refusing to replace existing qdisc on {device}: {existing.strip()}")
    tc("qdisc", "add", "dev", device, "root", "handle", "1:", "prio", "bands", "3")
    try:
        tc("qdisc", "add", "dev", device, "parent", "1:3", "handle", "30:", "netem", "loss", f"{loss_pct}%")
        tc("filter", "add", "dev", device, "protocol", "ip", "parent", "1:", "prio", "3", "flower",
           "dst_ip", f"{pbx_ip}/32", "ip_proto", "udp", "src_port", str(media_port), "flowid", "1:3")
    except Exception:
        tc("qdisc", "del", "dev", device, "root")
        raise
    return device


def disable_netem(device, log_path=None):
    if log_path:
        Path(log_path).write_text(tc("-s", "qdisc", "show", "dev", device, capture=True))
    tc("qdisc", "del", "dev", device, "root")


def listen(port, sink, stop):
    s = socket.create_connection(("127.0.0.1", port))
    s.settimeout(0.2)
    dec = kiss.Decoder()
    while not stop.is_set():
        try:
            data = s.recv(4096)
        except socket.timeout:
            continue
        if not data:
            break
        for _, cmd, payload in dec.feed(data):
            if cmd == kiss.DATA:
                sink.append(payload)
    s.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stations", type=int, default=3)
    ap.add_argument("--frames", type=int, default=10)
    ap.add_argument("--loss", type=float, default=0.0, help="simulated receive RTP loss at every station")
    ap.add_argument("--tx-silence-every", type=int, default=0,
                    help="station 0 replaces every Nth active outgoing RTP block with silence")
    ap.add_argument("--tx-drop-every", type=int, default=0,
                    help="station 0 omits every Nth active outgoing RTP packet")
    ap.add_argument("--overlap-talker", action="store_true",
                    help="with 3+ stations, station 2 transmits 200 ms after station 0, in full-duplex mode")
    ap.add_argument("--continuous-masker", action="store_true",
                    help="with 3+ stations, station 2 sends a continuous low-level 3 kHz tone")
    ap.add_argument("--speech-masker", metavar="WAV",
                    help="with 3+ stations, station 2 loops mono 16-bit speech audio during its idle RTP blocks")
    ap.add_argument("--masker-gain-db", type=float, default=-20.0,
                    help="overlap talker's transmit gain in dB (default: -20 dB)")
    ap.add_argument("--netem-loss-pct", type=float, default=0.0,
                    help="temporarily drop this percent of station 0 RTP packets using Linux tc netem")
    ap.add_argument("--netem-device", help="interface for tc (default: route interface to PBX)")
    ap.add_argument("--base-media-port", type=int, default=20000,
                    help="station 0 RTP port for tc matching; stations use successive ports")
    ap.add_argument("--room", default="7300@172.31.73.10")
    ap.add_argument("--tap")
    ap.add_argument("--spacing", type=float, default=3.0, help="mean seconds between a station's frames")
    ap.add_argument("--seed", type=int, default=1, help="seed for reproducible randomized frame scheduling")
    ap.add_argument("--txdelay", type=int, default=300)
    ap.add_argument("--modem", default="afsk1200")
    ap.add_argument("--collision-detect", action="store_true")
    ap.add_argument("--fx25", type=int, default=0)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--il2p", default="off")
    ap.add_argument("--iax", type=int, default=0, help="the first N stations dial over IAX2 (secret: HAM_MODEM_IAX_SECRET)")
    ap.add_argument("--base-port", type=int, default=18100)
    ap.add_argument("--log-dir", help="directory for per-station runtime logs (default: /tmp/ham-stationN.log)")
    args = ap.parse_args()
    if args.tx_silence_every and args.tx_drop_every:
        ap.error("choose either --tx-silence-every or --tx-drop-every")
    if args.overlap_talker and args.stations < 3:
        ap.error("--overlap-talker requires at least 3 stations")
    if args.continuous_masker and args.stations < 3:
        ap.error("--continuous-masker requires at least 3 stations")
    if args.speech_masker and args.stations < 3:
        ap.error("--speech-masker requires at least 3 stations")
    if sum(bool(x) for x in (args.overlap_talker, args.continuous_masker, args.speech_masker)) > 1:
        ap.error("choose one of --overlap-talker, --continuous-masker, or --speech-masker")
    if not 0 <= args.netem_loss_pct < 100:
        ap.error("--netem-loss-pct must be in [0, 100)")
    if args.netem_loss_pct and args.iax:
        ap.error("tc RTP impairment requires SIP stations")
    if args.netem_loss_pct and (args.base_media_port < 1024 or args.base_media_port + args.stations > 65535):
        ap.error("--base-media-port range must fit in 1024..65535")

    env = dict(os.environ, PYTHONPATH=str(ROOT))
    log_dir = Path(args.log_dir) if args.log_dir else None
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
    procs, logs = [], []
    netem_device = None
    for i in range(args.stations):
        leg = ["--iax-dial", args.room.split(":")[0]] if i < args.iax else ["--sip-dial", args.room]
        persist = 255 if args.overlap_talker and i in (0, 2) else 63
        cmd = [sys.executable, "-m", "hammodem.main", *leg, "--modem", args.modem,
               "--kiss-tcp", str(args.base_port + i), "--txdelay", str(args.txdelay), "--persist", str(persist),
               "--sim-loss", str(args.loss), "-m"]
        if args.netem_loss_pct:
            cmd += ["--media-port", str(args.base_media_port + i)]
        if i == 0 and args.tx_silence_every:
            cmd += ["--sim-tx-silence-every", str(args.tx_silence_every)]
        if i == 0 and args.tx_drop_every:
            cmd += ["--sim-tx-drop-every", str(args.tx_drop_every)]
        if args.overlap_talker and i == 2:
            cmd += ["--sim-tx-gain-db", str(args.masker_gain_db)]
        if args.continuous_masker and i == 2:
            cmd += ["--sim-mask-tone-hz", "3000", "--sim-mask-gain-db", str(args.masker_gain_db)]
        if args.speech_masker and i == 2:
            cmd += ["--sim-mask-wav", str(Path(args.speech_masker).resolve()),
                    "--sim-mask-gain-db", str(args.masker_gain_db)]
        if args.collision_detect:
            cmd.append("--collision-detect")
        if args.fx25:
            cmd += ["--fx25", str(args.fx25)]
        if args.repeat > 1:
            cmd += ["--repeat", str(args.repeat)]
        if args.il2p != "off":
            cmd += ["--il2p", args.il2p]
        if i == 0 and args.tap:
            cmd += ["--audio-tap", args.tap]
        log_path = log_dir / f"station{i}.log" if log_dir else Path(f"/tmp/ham-station{i}.log")
        log = log_path.open("w")
        logs.append(log)
        procs.append(subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT))
    try:
        time.sleep(3)
        dead = [i for i, p in enumerate(procs) if p.poll() is not None]
        if dead:
            for log in logs:
                log.flush()
            failed_log = log_dir / f"station{dead[0]}.log" if log_dir else Path(f"/tmp/ham-station{dead[0]}.log")
            tail = failed_log.read_text().strip().splitlines()[-1:]
            sys.exit(f"station {dead[0]} exited at startup: {tail[0] if tail else '(no output)'}")
        if args.netem_loss_pct:
            pbx_ip = socket.gethostbyname(args.room.rpartition("@")[2].split(":")[0])
            netem_device = enable_netem(args.netem_loss_pct, pbx_ip, args.base_media_port, args.netem_device)
            print(f"tc netem: {args.netem_loss_pct:g}% station 0 RTP loss on {netem_device}, "
                  f"UDP source port {args.base_media_port} to {pbx_ip}")
        stop = threading.Event()
        heard = [[] for _ in range(args.stations)]
        threads = [threading.Thread(target=listen, args=(args.base_port + i, heard[i], stop), daemon=True)
                   for i in range(args.stations)]
        for t in threads:
            t.start()
        senders = [socket.create_connection(("127.0.0.1", args.base_port + i)) for i in range(args.stations)]
        if args.overlap_talker:
            senders[2].sendall(kiss.encode(b"\x01", command=kiss.FULLDUPLEX)
                               + kiss.encode(b"\x00", command=kiss.TXDELAY))
        elif args.continuous_masker or args.speech_masker:
            senders[0].sendall(kiss.encode(b"\x01", command=kiss.FULLDUPLEX))
        sent = {i: [] for i in range(args.stations)}
        rng = random.Random(args.seed)
        if args.overlap_talker:
            schedule = [(k * args.spacing, 0) for k in range(args.frames)]
            schedule += [(k * args.spacing + 0.2, 2) for k in range(args.frames)]
            schedule.sort()
        elif args.continuous_masker or args.speech_masker:
            schedule = [(k * args.spacing, 0) for k in range(args.frames)]
        else:
            schedule = sorted((rng.uniform(0, args.frames * args.spacing), i) for i in range(args.stations)
                              for _ in range(args.frames))
        t0 = time.monotonic()
        for k, (at, i) in enumerate(schedule):
            time.sleep(max(0.0, t0 + at - time.monotonic()))
            frame = Frame.ui(f"N{i}TST", "APRS", f">frame {k} from station {i}", "").encode()
            sent[i].append(frame)
            senders[i].sendall(kiss.encode(frame))
        time.sleep(max(8.0, args.stations * 2.0))
        if netem_device:
            disable_netem(netem_device, log_dir / "netem_qdisc.txt" if log_dir else None)
            netem_device = None
        stop.set()
        for t in threads:
            t.join(1)
    finally:
        try:
            if netem_device:
                disable_netem(netem_device, log_dir / "netem_qdisc.txt" if log_dir else None)
        finally:
            for p in procs:
                p.terminate()
            for p in procs:
                p.wait(5)
            for log in logs:
                log.close()

    total = got = 0
    if args.overlap_talker:
        traffic = f"{args.stations} stations, {args.frames} frames from stations 0 and 2 (station 1 observer)"
    elif args.continuous_masker or args.speech_masker:
        traffic = f"{args.stations} stations, {args.frames} frames from station 0 (station 1 observer)"
    elif args.speech_masker:
        traffic = f"{args.stations} stations, {args.frames} frames from station 0 (station 1 observer)"
    else:
        traffic = f"{args.stations} stations, {args.frames} frames each"
    print(f"{args.modem}{' CSMA/CD' if args.collision_detect else ''}{f' FX.25/{args.fx25}' if args.fx25 else ''}{f' x{args.repeat}' if args.repeat > 1 else ''}{f' IL2P/{args.il2p}' if args.il2p != 'off' else ''}{f' ({args.iax} via IAX2)' if args.iax else ''}: "
          f"{traffic}, simulated loss {args.loss:.1%}"
          f"{' with controlled overlap' if args.overlap_talker else ''}"
          f"{' with a continuous station 2 masker' if args.continuous_masker else ''}"
          f"{' with a speech station 2 masker' if args.speech_masker else ''}"
          f"{f', netem loss {args.netem_loss_pct:g}%' if args.netem_loss_pct else ''}, schedule seed {args.seed}")
    print("receiver \\ sender  " + "  ".join(f"st{j:<4}" for j in range(args.stations)))
    for i in range(args.stations):
        row = []
        for j in range(args.stations):
            if i == j:
                row.append("  -   ")
                continue
            n = sum(f in heard[i] for f in sent[j])
            total += len(sent[j])
            got += n
            row.append(f"{n:>2}/{len(sent[j]):<3}")
        print(f"st{i:<16}" + "  ".join(row))
        own = sum(f in heard[i] for f in sent[i])
        if own:
            print(f"   (station {i} heard {own} of its own frames: the bridge echoed us?)")
    print(f"delivered {got}/{total} = {got / total:.1%}")


if __name__ == "__main__":
    main()
