#!/usr/bin/env python3
"""Compare clean and FX.25 delivery under reproducible randomized ConfBridge traffic schedules."""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--room", default="7300@172.31.73.10")
    ap.add_argument("--out", default="lab/results/randomized_schedule_2026_10_24")
    ap.add_argument("--seeds", default="7,19,41", help="comma-separated reproducible schedule seeds")
    ap.add_argument("--first-kiss-port", type=int, default=19200)
    args = ap.parse_args()
    try:
        seeds = [int(seed) for seed in args.seeds.split(",")]
    except ValueError:
        ap.error("--seeds must be comma-separated integers")
    if not seeds:
        ap.error("at least one seed is required")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary = out / "summary.txt"
    run_index = 0
    with summary.open("w") as report:
        report.write("Randomized ConfBridge schedule comparison\n"
                     "Stations: 3; frames per station: 10; spacing: 2 s; modem: AFSK1200; "
                     "channel: SIP/PCMU 8 kHz; conditions: clean, FX.25/32\n")
        for seed in seeds:
            for fec in (0, 32):
                run_index += 1
                name = f"seed{seed}_fx{fec or 'off'}"
                run_dir = out / name
                command = [sys.executable, "lab/confbridge_test.py", "--stations", "3", "--frames", "10",
                           "--spacing", "2", "--seed", str(seed), "--room", args.room,
                           "--base-port", str(args.first_kiss_port + 3 * (run_index - 1)),
                           "--log-dir", str(run_dir)]
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
