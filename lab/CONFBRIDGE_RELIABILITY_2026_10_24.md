# ConfBridge reliability trials — 2026-10-24

## Setup

The lab Asterisk PBX was healthy and the bridge was empty before the runs. Packet trials used two AFSK1200 SIP
stations in room 7300 (PCMU, 8 kHz), 10 UI frames per station, a seeded send schedule, and two repetitions of each
condition. Station 0's `--tx-silence-every N` setting replaced every Nth non-silent 20 ms outgoing RTP block with
zero PCM while retaining the RTP packet and clock. FX.25 was either disabled or enabled as FX.25/32. Each run
contained 20 expected cross-station deliveries.

This models an uplink that becomes silence in the bridge mix. The interval is over non-silent application blocks,
not a measured packet-loss probability. The actual silenced-block counts are listed below.

## Packet delivery matrix

| Station 0 impairment | TX silent blocks (two runs) | No FEC deliveries | FX.25/32 deliveries | FX.25 repairs (two runs) |
|---|---:|---:|---:|---:|
| None | 0 / 0 | 20/20, 20/20 | 20/20, 20/20 | 0 / 0 |
| Every 40th active block | 7 / 7 | 18/20, 18/20 | 20/20, 20/20 | 9 / 10 |
| Every 20th active block | 14 / 14 without FEC; 20 / 23 with FX.25 | 11/20, 12/20 | 20/20, 20/20 | 10 / 10 |

Every run reported zero bad FCS. RTP concealment was zero in all runs, consistent with the continuous RTP stream;
there were no late or duplicate RTP packets. Receiver silence-erasure counters include normal post-burst silence
inside the 80 ms hangover, so they exceed the injected block count. For example, at every-20 the no-FEC receiver
counted 49 inferred blocks in each run; the FX.25 receiver counted 36 and 42 while repairing 10 frames in both
runs. The counter demonstrates that the detector was active, while the repairs and delivery matrix show the FEC
path outcome.

The matrix indicates repeatable recovery for these controlled block-silence patterns: FX.25/32 delivered all
frames in both impaired conditions, while delivery without FEC declined as more blocks were silenced. It is a small
two-station trial with one seeded traffic schedule, not a broad estimate of real network performance. It does not
test other talkers masking the silence, different codecs, or random packet loss distributions.

## Reproduction and artifacts

Representative commands:

```sh
.venv/bin/python lab/confbridge_test.py --stations 2 --frames 10 --spacing 2 \
  --room 7300@172.31.73.10 --base-port 18400 --log-dir lab/results/confbridge_reliability_2026_10_24/r1_clean_fxoff
.venv/bin/python lab/confbridge_test.py --stations 2 --frames 10 --spacing 2 --tx-silence-every 40 \
  --fx25 32 --room 7300@172.31.73.10 --base-port 18412 --log-dir lab/results/confbridge_reliability_2026_10_24/r1_silence40_fx32
.venv/bin/python lab/confbridge_test.py --stations 2 --frames 10 --spacing 2 --tx-silence-every 20 \
  --fx25 32 --room 7300@172.31.73.10 --base-port 18420 --log-dir lab/results/confbridge_reliability_2026_10_24/r1_silence20_fx32
```

The full 12-run command/output transcript is in [summary.txt](results/confbridge_reliability_2026_10_24/summary.txt).
Per-station runtime logs are in the same results directory, grouped by condition and repetition.

## Live PACTOR-II/III ARQ retries

The same silent-block injector was applied to station A in the two-terminal HF link harness. The message contained
four copies of a 28-character phrase, forcing several ARQ data frames. Outcomes:

| Mode and cadence | A to B | B to A | Retry evidence | Disconnect |
|---|---|---|---|---|
| PACTOR-II, every 40th active block | Full payload | Reply delivered | 1 NACK retry during data; 1 timeout retry during disconnect | Completed |
| PACTOR-II, every 20th active block | Full payload | Reply delivered | 2 NACK retries during data | Completed |
| PACTOR-III, every 40th active block | Full payload | Reply delivered | 3 connection timeouts and 7 retries during data | Exhausted 8 retries; did not disconnect cleanly |

The PACTOR-III data exchange succeeded, but session teardown failed under this impairment. A separate stronger
PACTOR-II stress attempt with an eight-copy payload exhausted its retry budget and did not complete the reverse
turn ([captured stress output](results/confbridge_reliability_2026_10_24/pactor2_silence20.txt)); a four-copy payload
at the same cadence completed in the verified run above. Message size therefore affects the observed outcome.
These are local experimental modes and do not establish SCS/HAL interoperability.

Commands:

```sh
.venv/bin/python lab/pactor_confbridge.py --modem pactor2 --tx-silence-every 40
.venv/bin/python lab/pactor_confbridge.py --modem pactor2 --tx-silence-every 20
.venv/bin/python lab/pactor_confbridge.py --modem pactor3 --tx-silence-every 40
```

Captured terminal results are in [pactor2_silence40_verified.txt](results/confbridge_reliability_2026_10_24/pactor2_silence40_verified.txt),
[pactor2_silence20_verified.txt](results/confbridge_reliability_2026_10_24/pactor2_silence20_verified.txt), and
[pactor3_silence40_verified.txt](results/confbridge_reliability_2026_10_24/pactor3_silence40_verified.txt).

## Limits and follow-up

Silencing at the SIP sender creates valid RTP packets with zero audio; it does not drop network packets. Real uplink
loss can have different burst lengths and timing, and another station can keep the conference mix above the
silence threshold. A small clean-channel randomized offered-load comparison is recorded in the [randomized schedule
note](RANDOMIZED_SCHEDULE_2026_10_24.md); it showed mixed outcomes and is not a FEC comparison under loss. Further
speech samples, controlled talker masking, and a remote physical path remain needed before generalizing to live
network reliability.
