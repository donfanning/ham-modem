#!/usr/bin/env python3
"""A stand-in for an Asterisk ConfBridge: mixes raw s16le UDP audio from every participant.

Each participant (identified by source address) receives, every 20 ms, the sum of everyone else's latest block
-- never its own, as in ConfBridge -- so stations hear each other and collisions add like RF.

    audio_hub.py --port 7300 [--monitor 127.0.0.1:7355] [--loss 0.02]

``--monitor`` sends the full mix to a listener (e.g. Direwolf with ``ADEVICE udp:7355``). ``--loss`` drops each
incoming block with that probability (silence in its place, no concealment), like a lossy leg into the bridge.
"""

import argparse
import random
import select
import socket
import time
from collections import deque

import numpy as np


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=7300)
    ap.add_argument("--rate", type=int, default=8000)
    ap.add_argument("--monitor", metavar="HOST:PORT")
    ap.add_argument("--loss", type=float, default=0.0)
    args = ap.parse_args()
    n = args.rate // 50
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.port))
    sock.setblocking(False)
    monitor = None
    if args.monitor:
        host, _, port = args.monitor.rpartition(":")
        monitor = (host, int(port))
    queues: dict[tuple, deque] = {}
    last_heard: dict[tuple, float] = {}
    rng = random.Random()
    next_tick = time.monotonic()
    print(f"hub on udp:{args.port}, {args.rate} Hz", flush=True)
    while True:
        r, _, _ = select.select([sock], [], [], max(0.0, next_tick - time.monotonic()))
        if r:
            while True:
                try:
                    data, addr = sock.recvfrom(8192)
                except BlockingIOError:
                    break
                if addr not in queues:
                    print(f"join {addr[0]}:{addr[1]}", flush=True)
                    queues[addr] = deque(maxlen=10)
                last_heard[addr] = time.monotonic()
                block = np.frombuffer(data[:2 * n], "<i2").astype(np.int32)
                if args.loss and rng.random() < args.loss:
                    block = np.zeros(n, np.int32)
                queues[addr].append(np.pad(block, (0, n - len(block))))
        now = time.monotonic()
        if now < next_tick:
            continue
        next_tick = now + 0.02 if now - next_tick > 0.2 else next_tick + 0.02
        for addr in [a for a, t in last_heard.items() if now - t > 5]:
            print(f"leave {addr[0]}:{addr[1]}", flush=True)
            del queues[addr], last_heard[addr]
        latest = {a: (q.popleft() if q else np.zeros(n, np.int32)) for a, q in queues.items()}
        total = sum(latest.values(), np.zeros(n, np.int32))
        for addr, own in latest.items():
            sock.sendto(np.clip(total - own, -32768, 32767).astype("<i2").tobytes(), addr)
        if monitor:
            sock.sendto(np.clip(total, -32768, 32767).astype("<i2").tobytes(), monitor)


if __name__ == "__main__":
    main()
