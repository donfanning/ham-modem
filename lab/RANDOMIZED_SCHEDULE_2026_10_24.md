# Randomized ConfBridge offered-load schedules — 2026-10-24

## Setup

The ConfBridge harness now accepts `--seed` to select the randomized frame offer schedule. Three ham-modem
AFSK1200 stations shared the 7300 SIP/PCMU, 8 kHz room. Each station offered ten UI frames at random times over a
20-second window. Each of seeds 7, 19, and 41 was run once without FEC and once with FX.25/32, using the same
offered schedule for each pair. There was no injected RTP or audio loss, and CSMA/CD was disabled.

## Results

Delivery counts are cross-station frame receptions (each run has 60 expected receptions):

| Schedule seed | FEC off | FX.25/32 |
|---:|---:|---:|
| 7 | 60/60 (100.0%) | 54/60 (90.0%) |
| 19 | 54/60 (90.0%) | 58/60 (96.7%) |
| 41 | 60/60 (100.0%) | 54/60 (90.0%) |
| **Total** | **174/180 (96.7%)** | **166/180 (92.2%)** |

The direction of the difference varies by offered schedule. No FX.25 repairs were counted in these runs; the trial
did not inject errors and therefore does not establish a FEC benefit. Random simultaneous offers create a useful
contention stress case, but the results are too small and mixed to compare reliability or throughput between the
framing modes. The harness seed fixes frame offer times; CSMA persistence/backoff randomness and wall-clock audio
timing remain uncontrolled.

## Reproduction and artifacts

Run the six comparisons with:

```sh
.venv/bin/python lab/randomized_schedule_matrix.py --seeds 7,19,41
```

The driver records each full command and output in [summary.txt](results/randomized_schedule_2026_10_24/summary.txt).
Per-station logs are grouped under the same directory by seed and FEC setting. This Asterisk ConfBridge result is
an implementation-internal lab measurement, not hardware or remote-path interoperability evidence.

A follow-up with filtered station-side 3% RTP egress loss is recorded in the [randomized loss report](RANDOMIZED_LOSS_2026_10_24.md).
