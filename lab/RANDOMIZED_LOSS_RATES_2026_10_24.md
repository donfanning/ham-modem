# Randomized ConfBridge RTP loss rates — 2026-10-24

## Setup

This follow-up extends the [3% randomized RTP-loss run](RANDOMIZED_LOSS_2026_10_24.md) to requested 1% and 5%
loss. It used three ham-modem AFSK1200 stations in the same 7300 SIP/PCMU, 8 kHz ConfBridge room, with ten frames
per station and randomized offer seeds 7 and 19. The same offer seed was paired with FEC off and FX.25/32. A
filtered `tc netem` qdisc dropped station 0's UDP RTP egress toward the PBX. CSMA/CD was disabled.

## Results

| Requested loss | Seed | FEC | Cross-station receptions | Egress drops | Measured loss | FX.25 repairs |
|---:|---:|---|---:|---:|---:|---:|
| 1% | 7 | Off | 58/60 (96.7%) | 9/1,368 | 0.66% | 0 |
| 1% | 7 | FX.25/32 | 56/60 (93.3%) | 10/1,367 | 0.73% | 4 |
| 1% | 19 | Off | 56/60 (93.3%) | 15/1,382 | 1.09% | 0 |
| 1% | 19 | FX.25/32 | 56/60 (93.3%) | 12/1,385 | 0.87% | 2 |
| **1% total** | — | **Off** | **114/120 (95.0%)** | **24/2,750** | **0.87%** | **0** |
| **1% total** | — | **FX.25/32** | **112/120 (93.3%)** | **22/2,752** | **0.80%** | **6** |
| 5% | 7 | Off | 46/60 (76.7%) | 77/1,300 | 5.92% | 0 |
| 5% | 7 | FX.25/32 | 58/60 (96.7%) | 65/1,312 | 4.95% | 16 |
| 5% | 19 | Off | 44/60 (73.3%) | 74/1,324 | 5.59% | 0 |
| 5% | 19 | FX.25/32 | 60/60 (100.0%) | 75/1,322 | 5.67% | 16 |
| **5% total** | — | **Off** | **90/120 (75.0%)** | **151/2,624** | **5.75%** | **0** |
| **5% total** | — | **FX.25/32** | **118/120 (98.3%)** | **140/2,634** | **5.31%** | **32** |

At the requested 1% setting, FX.25/32 did not improve the aggregate delivery count in these two seeds, despite six
reported repairs. At 5%, it delivered 28 more receptions and receivers reported 32 repairs. These small samples
show a stronger benefit at this tested 5% condition, but do not establish a general performance curve. The
realized egress drop rate varied around the requested setting, and the schedule seed fixes frame offer times but
does not fix CSMA persistence/backoff or wall-clock audio timing. Only station 0's egress was impaired; this was
an isolated host bridge path, not RF or remote-network loss.

## Reproduction and artifacts

Run the 1% and 5% matrices with:

```sh
.venv/bin/python lab/randomized_loss_matrix.py --seeds 7,19 --loss-pct 1 --out lab/results/randomized_loss_1pct_2026_10_24 --first-kiss-port 19600 --first-media-port 21600
.venv/bin/python lab/randomized_loss_matrix.py --seeds 7,19 --loss-pct 5 --out lab/results/randomized_loss_5pct_2026_10_24 --first-kiss-port 19700 --first-media-port 21700
```

Each output directory contains the exact commands and results in `summary.txt`, per-station logs, and
`netem_qdisc.txt` snapshots. The temporary qdisc was removed after each run.
