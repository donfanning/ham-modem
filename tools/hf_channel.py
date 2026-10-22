#!/usr/bin/env python3
"""Pass a WAV file through the HF channel simulator (Watterson / CCIR 520).

    hf_channel.py in.wav out.wav --preset moderate --snr 10 [--offset 5] [--impulses 2] [--seed 1]

Presets: awgn, good, moderate, poor, flutter, midlat-disturbed, lowlat-disturbed (hammodem/hfchannel.py).
SNR is in a 3 kHz bandwidth, against the average power of the non-silent part of the input.
"""

import argparse
import math
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hammodem.hfchannel import PRESETS, HfChannel  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inp")
    ap.add_argument("out")
    ap.add_argument("--preset", choices=PRESETS, default="moderate")
    ap.add_argument("--snr", type=float, help="dB in 3 kHz (omit for no noise)")
    ap.add_argument("--offset", type=float, default=0.0, help="carrier frequency offset, Hz")
    ap.add_argument("--impulses", type=float, default=0.0, help="impulse-noise hits per second")
    ap.add_argument("--seed", type=int)
    args = ap.parse_args()
    with wave.open(args.inp) as w:
        fs, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2")[::ch].astype(float)
    active = x[np.abs(x) > 100]
    level = 10 * math.log10(float(np.mean(active ** 2)) / 32767 ** 2) if len(active) else -30.0
    chan = HfChannel(fs, args.preset, args.snr, args.offset, args.impulses, signal_dbfs=level, seed=args.seed)
    y = np.concatenate([chan.process(x[i:i + 160]) for i in range(0, len(x), 160)])
    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes(np.clip(np.round(y), -32768, 32767).astype("<i2").tobytes())
    print(f"{args.preset} ({PRESETS[args.preset].description}), SNR {args.snr} dB in 3 kHz, "
          f"signal {level:.1f} dBFS -> {args.out}")


if __name__ == "__main__":
    main()
