#!/usr/bin/env python3
"""Packet delivery through the HF channel simulator: modem x channel x SNR grid (offline).

    hf_bench.py [--modems afsk300,afsk1200] [--presets awgn,good,moderate,poor] [--snr 20,15,10,5] [--frames 20]
                [--fx25 0]
"""

import argparse
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hammodem.ax25 import Frame  # noqa: E402
from hammodem.hfchannel import HfChannel  # noqa: E402
from hammodem.modems import default_rate, make  # noqa: E402


def trial(modem: str, preset: str, snr: float, frames: int, fx25: int, seed: int) -> int:
    fs = default_rate(modem)
    mod, dem = make(modem, fs)
    mod.fx25 = fx25
    sent = [Frame.ui("N0CALL", "APRS", f">hf {seed} {i} " + "h" * 40).encode() for i in range(frames)]
    gap = np.zeros(fs // 2, np.int16)
    audio = np.concatenate([np.concatenate((mod.modulate([f], 0.3), gap)) for f in sent]).astype(float)
    active = audio[np.abs(audio) > 100]
    level = 10 * math.log10(float(np.mean(active ** 2)) / 32767 ** 2)
    ch = HfChannel(fs, preset, snr, signal_dbfs=level, seed=seed)
    got: list[bytes] = []
    for i in range(0, len(audio), 160):
        y = ch.process(audio[i:i + 160])
        got += dem.feed(np.clip(np.round(y), -32768, 32767).astype(np.int16))
    return sum(g in sent for g in got)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modems", default="afsk300,afsk1200")
    ap.add_argument("--presets", default="awgn,good,moderate,poor")
    ap.add_argument("--snr", default="20,15,10,5")
    ap.add_argument("--frames", type=int, default=20)
    ap.add_argument("--fx25", type=int, default=0)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    snrs = [float(s) for s in args.snr.split(",")]
    print(f"frames delivered of {args.frames} (~60-byte UI frames, 300 ms TXDELAY), FX.25 {args.fx25 or 'off'}")
    print(f"{'modem':10s} {'channel':10s} " + " ".join(f"{s:>6.0f}dB" for s in snrs))
    for modem in args.modems.split(","):
        for preset in args.presets.split(","):
            row = [trial(modem, preset, s, args.frames, args.fx25, args.seed) for s in snrs]
            print(f"{modem:10s} {preset:10s} " + " ".join(f"{r:>8d}" for r in row), flush=True)


if __name__ == "__main__":
    main()
