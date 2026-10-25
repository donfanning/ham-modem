# RTP packet loss and multi-talker erasure masking — 2026-10-24

## What changed

`--sim-tx-drop-every N` now omits every Nth non-silent RTP datagram at the sending station. The RTP sequence number
and timestamp advance for the omitted packet, so the next packet exposes the actual RTP gap to Asterisk. This differs
from `--sim-tx-silence-every N`, which sends the RTP packet with zero PCM. Sender and receiver shutdown stats report
the number of injected drops and inferred silence-erasure blocks.

The ConfBridge harness also supports two controlled third-station masking patterns:

- `--overlap-talker`: station 2 sends packet frames 200 ms after station 0, in full-duplex mode, at a configurable
  lower level. This tests overlapping packetized traffic and can introduce collisions.
- `--continuous-masker`: station 2 sends continuous 3 kHz audio at -20 dBFS while station 0 sends frames. Station 0
  uses full-duplex mode to transmit through the masker. This isolates the detector's response when another talker
  keeps the mixed audio above the silence threshold.

The RTP-drop simulation omits UDP sends in the station process before the packet reaches the socket. It preserves
RTP sequence/timestamp progression and creates a genuine missing station-to-PBX RTP datagram, but it is not a
router-level impairment such as `tc netem`.

## Two-station RTP-drop comparison

Room 7300, AFSK1200, two stations, 10 frames per station, `--tx-drop-every 40`, two repetitions:

| FEC | Delivery run 1 | Delivery run 2 | Receiver evidence |
|---|---:|---:|---|
| Off | 18/20 | 16/20 | Station 1 received 8/10 then 6/10 frames from station 0; 2 then 1 bad FCS |
| FX.25/32 | 20/20 | 20/20 | Station 1 repaired 9 frames in each run; bad FCS 0 |

Station 0 omitted 6–7 RTP datagrams without FEC and 11–12 with FX.25/32. Station 1 reported zero RTP concealment,
late packets, and duplicates, consistent with Asterisk continuing to send the mixed downlink. The no-FEC outcome
matches the earlier two-station zero-audio-block run at the same interval (18/20 twice); FX.25/32 recovered all
frames in both impairment models.

## Third talker and continuous masking

### Continuous masker

The continuous-masker conditions used station 2 as a constant 3 kHz source at -20 dBFS, station 0 as the impaired
sender, and station 1 as receiver. Station 0 transmitted 10 frames. The clean FX.25/32 control delivered 10/10 with
zero silence-erasure blocks at station 1.

Each impairment/FEC condition was repeated twice:

| Station 0 impairment | No-FEC delivery to station 1 | FX.25/32 delivery to station 1 | FX.25 repairs | Inferred silence erasures |
|---|---:|---:|---:|---:|
| Every 40th active block replaced by zero audio | 7/10, 7/10 | 10/10, 10/10 | 8, 8 | 0 in all four runs |
| Every 40th active RTP datagram omitted | 7/10, 7/10 | 7/10, 6/10 | 3, 2 | 0 in all four runs |

The TNC sender omitted 7 or 13 packets per run depending on the mode's transmitted burst length. The receiver had
zero RTP concealment in these runs because the bridge's own downlink remained continuous. The continuous masker kept
station 1's mixed-audio level above the silence threshold, so the receiver inferred no silence erasures. In this
configuration, FX.25/32 delivered all zero-audio-block cases but fewer frames when the corresponding RTP datagrams
were absent under the masker. These measurements do not establish that the successful zero-audio repairs used
erasure markers; the receiver's inferred-erasure counter was zero in those runs.

This is direct evidence that another talker's audio can mask an uplink loss that would otherwise be marked as
erased. The 3 kHz tone is a controlled, out-of-band masker rather than speech or a natural third modem waveform; it
isolates the level detector and does not estimate real-talk audio distributions.

### Packetized overlap probe

Station 2 also sent lower-level frames 200 ms after station 0 in two repetitions of each silence/drop and FEC
condition. Station 1 delivered 7/10 frames without FEC and 10/10 with FX.25/32 for both impairment types. Its
inferred-erasure counter was 42–43 blocks and FX.25 repair counts were the same for zero-audio and dropped packets.
The packetized overlap did not isolate loss masking as cleanly because its bursts also changed collision behavior;
the continuous-masker experiment above is the diagnostic comparison.

## Reproduction and artifacts

Representative commands:

```sh
.venv/bin/python lab/confbridge_test.py --stations 2 --frames 10 --spacing 2 \
  --tx-drop-every 40 --fx25 32 --room 7300@172.31.73.10 --base-port 18604 \
  --log-dir lab/results/packet_drop_masking_2026_10_24/r1_two_drop40_fx32
.venv/bin/python lab/confbridge_test.py --stations 3 --frames 10 --spacing 2 \
  --continuous-masker --masker-gain-db -20 --tx-silence-every 40 --fx25 32 \
  --room 7300@172.31.73.10 --base-port 18804 \
  --log-dir lab/results/packet_drop_masking_2026_10_24/continuous_masker/r1_silence40_fx32
.venv/bin/python lab/confbridge_test.py --stations 3 --frames 10 --spacing 2 \
  --continuous-masker --masker-gain-db -20 --tx-drop-every 40 --fx25 32 \
  --room 7300@172.31.73.10 --base-port 18812 \
  --log-dir lab/results/packet_drop_masking_2026_10_24/continuous_masker/r1_drop40_fx32
```

The repeated two-station and packetized-overlap trials are in [their 12-run summary](results/packet_drop_masking_2026_10_24/summary.txt).
The repeated continuous-masker trials are in [their eight-run summary](results/packet_drop_masking_2026_10_24/continuous_masker/summary.txt).
Both directories contain per-station runtime logs with transmit drops, erasures, FEC repairs, bad FCS, and RTP stats.

## Limits

The comparison used one seeded station schedule and one Asterisk/PCMU path. The packet-drop hook omits datagrams in
the application before UDP send, not through a physical interface impairment. The continuous masker is deliberately
artificial and at a fixed level. Repeat with randomized schedules, speech or modem audio, and a network-level
uplink drop before making broader performance claims.
