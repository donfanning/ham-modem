# Speech masking and network-layer RTP loss — 2026-10-24

## Setup

The ConfBridge harness can play a speech WAV from station 2 while station 0 transmits frames and station 1 observes
the bridge mix. `--speech-masker WAV` replaces station 2's idle audio blocks with looping audio from a mono,
16-bit PCM WAV at the selected codec rate. Station 0 is put in full-duplex mode so the speech and modem can overlap.

The test fixture is a human reading from LJSpeech 1.1, sample LJ001-0001. It was converted to mono, 16-bit, 8 kHz
PCM for the PCMU path. The file's measured mean and peak levels before lab gain were -19.6 dBFS and -0.9 dBFS;
trials used -5 dB gain (approximately -24.6 dBFS mean and -5.9 dBFS peak). LJSpeech describes its LibriVox
recordings as public domain; provenance and conversion instructions are in [the asset README](assets/README.md).

`--netem-loss-pct 3` installs a temporary `tc netem` qdisc after stations connect. A flower filter matches only
UDP from station 0's configured RTP source port to the PBX address. The harness checks that the selected interface
has its default `noqueue` root qdisc before installing a three-band priority qdisc, records qdisc counters, and
removes the temporary qdisc before shutting down the stations. This requires `tc`, `ip`, and root or passwordless
`sudo` access. In this lab the egress device was Docker bridge `br-5562fa077726`, and the PBX was `172.31.73.10`.

## Repeated comparison

Each condition used three stations, AFSK1200 over PCMU, 10 frames from station 0 at two-second spacing, station 1
as the observer, the speech sample at -5 dB, and two repetitions. Loss conditions were either every 40th active
station-0 audio block replaced with zero PCM while retaining RTP, or 3% random drops on station-0 RTP at Linux
egress. The table reports station 1's frames decoded from station 0.

| Condition | FEC | Delivery, run 1 / run 2 | FX.25 repairs, run 1 / run 2 | Inferred silence blocks, run 1 / run 2 | Other evidence |
|---|---|---:|---:|---:|---|
| Clean | Off | 10/10, 10/10 | — | 30, 30 | bad FCS 3, 3 |
| Clean | FX.25/32 | 10/10, 10/10 | 1, 1 | 29, 28 | bad FCS 2, 2 |
| Every 40th active block zeroed | Off | 7/10, 7/10 | — | 31, 30 | station 0 sent 7 zero blocks each run; bad FCS 3, 3 |
| Every 40th active block zeroed | FX.25/32 | 10/10, 10/10 | 6, 6 | 28, 28 | station 0 sent 13 zero blocks each run; bad FCS 3, 3 |
| 3% RTP netem loss | Off | 5/10, 7/10 | — | 31, 31 | qdisc dropped 41/1260 and 45/1256 packets; bad FCS 3, 3 |
| 3% RTP netem loss | FX.25/32 | 10/10, 10/10 | 8, 7 | 29, 29 | qdisc dropped 40/1261 and 31/1271 packets; bad FCS 3, 2 |

The qdisc's observed loss was 2.4–3.6% across the two trials per FEC setting. The receiver reported zero RTP
concealment, late packets, and duplicates in every run: the bridge continued sending its mixed downlink even when
station-0 uplink datagrams were dropped. FX.25/32 delivered all frames in these four impaired trials, with 6–8
repair counts. Without FEC, the zero-audio condition was 7/10 in both runs; random network loss was 5/10 and 7/10.

## What the speech masker showed

Station 1 counted 28–31 inferred silence-erasure blocks in every condition, including clean runs. The counter
accumulates over the whole trial, including quiet gaps between modem frames and pauses in the spoken sample, so
these totals do not identify whether a particular damaged modem block was masked. Unlike the continuous 3 kHz
control, this speech sample did not hold the mixed audio above the silence detector threshold continuously. The
measurements therefore do not show that speech reliably hides loss markers; the limited repeated delivery data do
show that FX.25/32 recovered all decoded frames under both payload-silence and network-drop impairments.

The speech fixture is one speaker reading a passage, played in a loop at one gain. It is not conversational speech,
multiple voices, or a speech-level distribution. The network impairment drops packets on the Linux bridge egress
inside the lab host; it is network-layer loss after RTP framing but not a loss experiment on a remote physical path.
Repeat with more speech samples, randomized timing, additional loss rates, and a remote station path before making
broader claims.

## Reproduction and artifacts

Run the full 12-trial comparison with:

```sh
.venv/bin/python lab/speech_netem_matrix.py
```

The matrix driver preserves the command and console result for each run in [summary.txt](results/speech_mask_netem_2026_10_24/summary.txt).
The same directory contains the individual station logs and, for netem trials, `netem_qdisc.txt` counter snapshots.
