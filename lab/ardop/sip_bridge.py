"""Put ardopcf (12 kHz UDP audio via relay.py) into a ConfBridge room: a SIP leg at 8 kHz G.711 with resampling.

    PYTHONPATH=. python lab/ardop/sip_bridge.py ROOM@PBX [--local-ip IP] [--relay HOST:PORT] [--listen PORT]

Audio from the room (8 kHz) is resampled to 12 kHz and sent to relay.py in the ardopcf container; ardopcf's
transmit audio comes back the other way. Like any station it never hears itself (ConfBridge does not echo).
"""
import argparse
import select
import socket
import time

import numpy as np

from hammodem.ardop.stream import Resampler
from hammodem.audio.sip import SipLeg

ap = argparse.ArgumentParser()
ap.add_argument("room")
ap.add_argument("--local-ip")
ap.add_argument("--relay", default="172.31.73.40:7400")
ap.add_argument("--listen", type=int, default=7401)
ap.add_argument("--bind", default="172.31.73.1")
args = ap.parse_args()

leg = SipLeg(dial=args.room, local_ip=args.local_ip or args.bind)
up, down = Resampler(3, 2), Resampler(2, 3)             # 8k -> 12k, 12k -> 8k
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((args.bind, args.listen))
sock.setblocking(False)
relay = (args.relay.rpartition(":")[0], int(args.relay.rpartition(":")[2]))
to_room = np.zeros(0)
to_relay = np.zeros(0)
n = leg.frame_samples
next_tx = time.monotonic()
print(f"bridging {leg.name} <-> relay {args.relay}", flush=True)
while True:
    r, _, _ = select.select([sock, *leg.fds()], [], [], max(0.0, next_tx - time.monotonic()))
    if sock in r:
        while True:
            try:
                data, _ = sock.recvfrom(4096)
            except BlockingIOError:
                break
            to_room = np.concatenate((to_room, down.process(np.frombuffer(data, "<i2").astype(float))))
            if len(to_room) > 8000:                      # > 1 s behind: drop (clock skew)
                to_room = to_room[-1600:]
    for block in leg.service(r):
        to_relay = np.concatenate((to_relay, up.process(np.asarray(block, dtype=float))))
    while len(to_relay) >= 240:
        sock.sendto(np.round(to_relay[:240]).astype("<i2").tobytes(), relay)
        to_relay = to_relay[240:]
    now = time.monotonic()
    if now - next_tx > 0.2:
        next_tx = now
    while now >= next_tx:
        out = to_room[:n]
        to_room = to_room[n:]
        if len(out) < n:
            out = np.concatenate((out, np.zeros(n - len(out))))
        leg.send(np.clip(np.round(out), -32768, 32767).astype(np.int16))
        next_tx += n / 8000
