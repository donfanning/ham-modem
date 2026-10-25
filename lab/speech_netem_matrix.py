#!/usr/bin/env python3
"""Repeated speech-masker comparison: zero-audio blocks versus station-side tc netem RTP loss."""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--room", default="7300@172.31.73.10")
    ap.add_argument("--out", default="lab/results/speech_mask_netem_2026_10_24")
    ap.add_argument("--speech-wav", default="lab/assets/speech_mask_ljspeech_lj001-0001_8k.wav")
    ap.add_argument("--first-kiss-port", type=int, default=19000)
    ap.add_argument("--first-media-port", type=int, default=20000)
    ap.add_argument("--start-repetition", type=int, choices=(1, 2), default=1)
    ap.add_argument("--conditions", default="clean,silence40,netem3",
                    help="comma-separated subset of clean,silence40,netem3")
    ap.add_argument("--append-summary", action="store_true")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary = out / "summary.txt"
    conditions = args.conditions.split(",")
    if not conditions or any(c not in ("clean", "silence40", "netem3") for c in conditions):
        ap.error("--conditions must be a comma-separated subset of clean,silence40,netem3")
    run_index = 0
    with summary.open("a" if args.append_summary else "w") as report:
        for repetition in range(args.start_repetition, 3):
            for condition in conditions:
                for fec in (0, 32):
                    run_index += 1
                    name = f"r{repetition}_{condition}_fx{fec or 'off'}"
                    run_dir = out / name
                    command = [sys.executable, "lab/confbridge_test.py", "--stations", "3", "--frames", "10",
                               "--spacing", "2", "--speech-masker", args.speech_wav, "--masker-gain-db", "-5",
                               "--room", args.room, "--base-port", str(args.first_kiss_port + 4 * (run_index - 1)),
                               "--base-media-port", str(args.first_media_port + 4 * (run_index - 1)),
                               "--log-dir", str(run_dir)]
                    if condition == "silence40":
                        command += ["--tx-silence-every", "40"]
                    elif condition == "netem3":
                        command += ["--netem-loss-pct", "3"]
                    if fec:
                        command += ["--fx25", str(fec)]
                    report.write(f"\n[{name}]\nCOMMAND: {' '.join(command)}\n")
                    report.flush()
                    print(f"[{name}] running", flush=True)
                    result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT)
                    report.write(f"EXIT: {result.returncode}\n{result.stdout}\n")
                    report.flush()
                    print(result.stdout, end="", flush=True)
                    if result.returncode:
                        print(f"run failed; see {summary}", file=sys.stderr)
                        return result.returncode
        report.write("\nCompleted successfully.\n")
    print(f"matrix summary: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
