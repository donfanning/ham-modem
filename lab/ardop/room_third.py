"""A third station in a ConfBridge room (8 kHz G.711 SIP leg), for the ARDOP lab runs.

    PYTHONPATH=. python lab/ardop/room_third.py ROOM@PBX monitor [--local-ip IP]
    PYTHONPATH=. python lab/ardop/room_third.py ROOM@PBX jam [--burst S] [--period S] [--kind tone|noise] [--start S]

monitor: listens only and prints every ARDOP frame it can decode from the mix (it never transmits).
jam:     transmits a burst of a carrier (1500 Hz) or band-limited noise every PERIOD seconds, as another user on the
         channel would; the room mixes it with the others, like a collision on RF.
"""
import argparse
import select
import time

import numpy as np

from hammodem.ardop import codec
from hammodem.ardop.stream import Phy
from hammodem.audio.sip import SipLeg

ap = argparse.ArgumentParser()
ap.add_argument("room")
ap.add_argument("mode", choices=("monitor", "jam"))
ap.add_argument("--local-ip")
ap.add_argument("--burst", type=float, default=2.0)
ap.add_argument("--period", type=float, default=20.0)
ap.add_argument("--start", type=float, default=30.0)
ap.add_argument("--kind", choices=("tone", "noise"), default="tone")
ap.add_argument("--level", type=int, default=3000)
args = ap.parse_args()

leg = SipLeg(dial=args.room, local_ip=args.local_ip)
n = leg.frame_samples
phy = Phy(8000)
rng = np.random.default_rng(7)
t0 = time.monotonic()
next_tx = t0
seen = 0
print(f"{args.mode} in {args.room}: {leg.name}", flush=True)
while True:
    r, _, _ = select.select(leg.fds(), [], [], max(0.0, next_tx - time.monotonic()))
    for block in leg.service(r):
        if args.mode == "monitor":
            for d in phy.feed(np.asarray(block, dtype=float)):
                seen += 1
                if d.ok and d.extra.get("caller") and d.extra.get("target"):      # learn the session ID from the call
                    phy.set_sessions((codec.session_id(d.extra["caller"].upper(), d.extra["target"].upper()),))
                print(f"{time.monotonic() - t0:7.1f} {d.name:16s} ok={d.ok} session={d.session:02X}", flush=True)
    now = time.monotonic()
    if now - next_tx > 0.2:
        next_tx = now
    while now >= next_tx:
        out = np.zeros(n, dtype=np.int16)
        t = next_tx - t0 - args.start
        if args.mode == "jam" and t >= 0 and t % args.period < args.burst:
            if args.kind == "tone":
                k = np.arange(n) + int(t * 8000)
                out = (args.level * np.sin(2 * np.pi * 1500 * k / 8000)).astype(np.int16)
            else:
                out = np.clip(rng.normal(0, args.level / 2, n), -32768, 32767).astype(np.int16)
        leg.send(out)
        next_tx += n / 8000
