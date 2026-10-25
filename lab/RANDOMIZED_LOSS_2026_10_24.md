# Randomized ConfBridge schedules with RTP loss — 2026-10-24

## Setup

Three ham-modem AFSK1200 stations shared the 7300 SIP/PCMU, 8 kHz ConfBridge room. Each offered ten UI frames
over a randomized 20-second schedule. Seeds 7 and 19 were each run with FEC off and FX.25/32. The same offered
frame schedule was used for each pair. A `tc netem loss 3%` qdisc was attached only to station 0's UDP RTP
egress toward the PBX. CSMA/CD was disabled, and there was no audio-silence injection.

## Results

| Schedule seed | FEC | Cross-station receptions | Filtered egress packets dropped | Measured drop rate | FX.25 repairs at receivers |
|---:|---|---:|---:|---:|---:|
| 7 | Off | 50/60 (83.3%) | 46/1,331 | 3.5% | 0 |
| 7 | FX.25/32 | 52/60 (86.7%) | 33/1,344 | 2.5% | 4 |
| 19 | Off | 54/60 (90.0%) | 36/1,362 | 2.6% | 0 |
| 19 | FX.25/32 | 56/60 (93.3%) | 25/1,373 | 1.8% | 6 |
| **Total** | **Off** | **104/120 (86.7%)** | **82/2,693** | **3.0%** | **0** |
| **Total** | **FX.25/32** | **108/120 (90.0%)** | **58/2,717** | **2.1%** | **10** |

FX.25/32 delivered four more cross-station receptions across these two matched seeds and receiver logs reported
ten repairs. The qdisc's realized drop rates varied from 1.8% to 3.5% around the requested 3%; the egress drops
were independently counted in each run. This is a small sample and does not establish a general improvement.
Only station 0's egress was impaired, the offered schedule seed does not seed CSMA persistence/backoff, and
wall-clock audio timing remains variable. The experiment measures an isolated Linux bridge path, not remote RF
or a remote internet connection.

## Reproduction and artifacts

Run the four comparisons with:

```sh
.venv/bin/python lab/randomized_loss_matrix.py --seeds 7,19 --loss-pct 3
```

The driver writes commands and complete harness output to [summary.txt](results/randomized_loss_2026_10_24/summary.txt).
Per-station logs and `netem_qdisc.txt` snapshots are grouped by seed and FEC setting in the same directory. The
temporary qdisc was removed after the runs; the bridge root qdisc returned to `noqueue`.

Additional 1% and 5% trials using the same two offer seeds are in the [multi-rate follow-up](RANDOMIZED_LOSS_RATES_2026_10_24.md).
