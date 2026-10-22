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
    ap.add_argument("--room", default="7300@172.31.73.10")
    ap.add_argument("--tap")
    ap.add_argument("--spacing", type=float, default=3.0, help="mean seconds between a station's frames")
    ap.add_argument("--txdelay", type=int, default=300)
    ap.add_argument("--modem", default="afsk1200")
    ap.add_argument("--collision-detect", action="store_true")
    ap.add_argument("--base-port", type=int, default=18100)
    args = ap.parse_args()

    env = dict(os.environ, PYTHONPATH=str(ROOT))
    procs, logs = [], []
    for i in range(args.stations):
        cmd = [sys.executable, "-m", "hammodem.main", "--sip-dial", args.room, "--modem", args.modem,
               "--kiss-tcp", str(args.base_port + i), "--txdelay", str(args.txdelay), "--sim-loss", str(args.loss), "-m"]
        if args.collision_detect:
            cmd.append("--collision-detect")
        if i == 0 and args.tap:
            cmd += ["--audio-tap", args.tap]
        log = open(f"/tmp/ham-station{i}.log", "w")
        logs.append(log)
        procs.append(subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT))
    try:
        time.sleep(3)
        dead = [i for i, p in enumerate(procs) if p.poll() is not None]
        if dead:
            for log in logs:
                log.flush()
            tail = Path(f"/tmp/ham-station{dead[0]}.log").read_text().strip().splitlines()[-1:]
            sys.exit(f"station {dead[0]} exited at startup: {tail[0] if tail else '(no output)'}")
        stop = threading.Event()
        heard = [[] for _ in range(args.stations)]
        threads = [threading.Thread(target=listen, args=(args.base_port + i, heard[i], stop), daemon=True)
                   for i in range(args.stations)]
        for t in threads:
            t.start()
        senders = [socket.create_connection(("127.0.0.1", args.base_port + i)) for i in range(args.stations)]
        sent = {i: [] for i in range(args.stations)}
        rng = random.Random(1)
        schedule = sorted((rng.uniform(0, args.frames * args.spacing), i) for i in range(args.stations)
                          for _ in range(args.frames))
        t0 = time.monotonic()
        for k, (at, i) in enumerate(schedule):
            time.sleep(max(0.0, t0 + at - time.monotonic()))
            frame = Frame.ui(f"N{i}TST", "APRS", f">frame {k} from station {i}", "").encode()
            sent[i].append(frame)
            senders[i].sendall(kiss.encode(frame))
        time.sleep(max(8.0, args.stations * 2.0))
        stop.set()
        for t in threads:
            t.join(1)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(5)
        for log in logs:
            log.close()

    total = got = 0
    print(f"{args.modem}{' CSMA/CD' if args.collision_detect else ''}: {args.stations} stations, {args.frames} frames each, simulated loss {args.loss:.1%}")
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
