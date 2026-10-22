#!/usr/bin/env python3
"""Generate or decode packet-modem audio (AFSK 1200/300, G3RUH 9600) as WAV files (offline benchmark and Direwolf interop).

    afsk_wav.py gen out.wav [--modem afsk1200] [--rate 8000] [--count 100]
    afsk_wav.py decode in.wav [--modem afsk1200] [-v]

``decode`` accepts any rate, mono or stereo (left channel), 16-bit PCM; it prints the frame count, like
Direwolf's ``atest``.
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hammodem.ax25 import Ax25Error, Frame  # noqa: E402
from hammodem.modems import MODEMS, make  # noqa: E402


def gen(args: argparse.Namespace) -> None:
    mod, _ = make(args.modem, args.rate)
    mod.fx25 = args.fx25
    mod.il2p = {"off": None, "on": False, "max": True}[args.il2p]
    gap = np.zeros(int(args.rate * 0.25), np.int16)
    chunks = []
    for i in range(args.count):
        info = f"!4903.50N/07201.75W-ham-modem test frame {i:04d}"
        chunks += [mod.modulate([Frame.ui(args.src, "APRS", info, "WIDE1-1").encode()], args.txdelay), gap]
    with wave.open(args.wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(args.rate)
        w.writeframes(np.concatenate(chunks).astype("<i2").tobytes())
    print(f"wrote {args.count} frames to {args.wav}")


def decode(args: argparse.Namespace) -> None:
    with wave.open(args.wav, "rb") as w:
        if w.getsampwidth() != 2:
            sys.exit("only 16-bit PCM WAV is supported")
        rate, channels = w.getframerate(), w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2")[::channels]
    _, dem = make(args.modem, rate)
    block = rate // 50
    count = 0
    t = time.perf_counter()
    for i in range(0, len(pcm), block):
        for raw in dem.feed(pcm[i:i + block]):
            count += 1
            if args.verbose:
                try:
                    text = Frame.decode(raw).monitor()
                except Ax25Error as exc:
                    text = f"<undecodable: {exc}>"
                print(f"{(i / rate):9.2f}s  {text}")
    elapsed = time.perf_counter() - t
    print(f"{count} packets decoded in {elapsed:.1f} s ({len(pcm) / rate:.0f} s of audio, "
          f"{elapsed / (len(pcm) / rate) * 100:.1f}% CPU)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("wav")
    g.add_argument("--modem", choices=MODEMS, default="afsk1200")
    g.add_argument("--rate", type=int, default=8000)
    g.add_argument("--count", type=int, default=100)
    g.add_argument("--src", default="N0CALL")
    g.add_argument("--txdelay", type=float, default=0.3)
    g.add_argument("--fx25", type=int, choices=(0, 16, 32, 64), default=0, help="FX.25 check bytes (0 = off)")
    g.add_argument("--il2p", choices=("off", "on", "max"), default="off")
    g.set_defaults(func=gen)
    d = sub.add_parser("decode")
    d.add_argument("wav")
    d.add_argument("--modem", choices=MODEMS, default="afsk1200")
    d.add_argument("-v", "--verbose", action="store_true")
    d.set_defaults(func=decode)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
