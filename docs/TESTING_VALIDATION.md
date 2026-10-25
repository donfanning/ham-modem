# Testing and validation

Last reviewed: 2026-10-24. This page distinguishes repeatable repository tests from recorded lab observations and
hardware interoperability. A local loopback or a VoIP peer test does not establish interoperability with a radio
vendor's equipment.

## Repeatable checks

Run the repository unit suite from the project root using the project virtual environment:

```sh
.venv/bin/python -m unittest discover -s tests -t .
```

The suite is organized under `tests/`; mode-specific files include `test_pactor2.py`, `test_pactor3.py`,
`test_clover2.py`, `test_fax.py`, and `test_sstv.py`. The latest full suite passed **652 tests** on 2026-10-24 in
66.599 seconds. Earlier figures of 651, 650, 649, 645, 644, 643, and 629 passing tests are historical reports and have been superseded.

## HF ARQ and image modes

| Feature | Evidence recorded | Limit |
|---|---|---|
| PACTOR-II local link | On 2026-10-24, live ConfBridge runs with injected silent RTP blocks delivered multi-frame text in both directions, with 1–2 NACK retries. | At stronger load, an eight-copy stress message exhausted retries; this is not SCS hardware interoperability. See the [live reliability report](../lab/CONFBRIDGE_RELIABILITY_2026_10_24.md). |
| PACTOR-III local link | On 2026-10-24, a live ConfBridge run delivered multi-frame text in both directions after retries. | Teardown exhausted retries under the tested impairment; this is not SCS hardware interoperability. See the [live reliability report](../lab/CONFBRIDGE_RELIABILITY_2026_10_24.md). |
| Clover-II local modes | Implemented modes are QPSM, BPSM, PSM2D, FSM2D, PSM8, PSM16, P8A2, and P16A4. | Unit-test coverage is represented by `tests/test_clover2.py`; live link performance and HAL interoperability are not established here. |
| PACTOR-I, AMTOR, ARDOP | Earlier project notes report implementations and unit tests. | No recent live result is documented on this page. Consult the dated notes under `lab/` for historical evidence. |
| FAX / SSTV | Offline encode/decode tests are in `tests/test_fax.py` and `tests/test_sstv.py`; CLI entry point is `ham-image`. | No live image transfer result is documented here. |

An earlier 2026-10-24 PACTOR-II/III conference summary was referenced at
`/tmp/hf_arq_cond_1792878315/summary.txt`, which is absent in the current environment. The later repeatable live
trials linked above have committed commands, terminal output, and configuration under `lab/`.

The deterministic retry and combining regression suite is reproducible and passed **171 tests** on 2026-10-24.
It simulates a lost ACK in the local link loop and verifies single delivery after retry for both PACTOR-II and
PACTOR-III. See the [ARQ reliability lab note](../lab/ARQ_RELIABILITY_2026_10_24.md) for the command and scope;
this does not replace a live VoIP retry test.

On 2026-10-24, the ARQ suite was extended with retry-exhaustion behavior for PACTOR-II and PACTOR-III and repeated
duplicate-frame ACK/delivery suppression. The targeted command passed **172 tests** in 7.363 seconds; the full
repository suite passed **645 tests** in 67.720 seconds. Exact commands and limits are in the [ARQ failure-path lab
note](../lab/ARQ_FAILURE_PATHS_2026_10_24.md).

## Keyboard and messaging modes

An Asterisk bidirectional trial was recorded at `/tmp/kb_bidirect_1792875946/summary.txt` on 2026-10-24. The log
is not a successful exchange report: it contains missing-file and I/O errors for PSK31, PSK63, MFSK16, and Olivia,
and reports RTTY PTY creation failure. Other modes were not covered by the captured run. The ephemeral log should
not be used to claim that all modes start or exchange text correctly.

| Mode | Repository unit test | Live result in captured trial |
|---|---|---|
| RTTY | `tests/test_rtty.py` | PTY creation failed |
| PSK31 / PSK63 | `tests/test_psk31.py` | Attempted; capture has missing-file/I/O errors, no verified delivery |
| MFSK16 | `tests/test_mfsk16.py` | Attempted; capture has missing-file/I/O errors, no verified delivery |
| Olivia / Contestia | `tests/test_olivia.py` | Olivia attempt has missing-file/I/O errors; Contestia not covered |
| DominoEX / THOR | `tests/test_dominoex.py` | Not covered |
| JS8 | `tests/test_js8.py` | Not covered |
| MIL-STD-188-110 / STANAG 4285 | `tests/test_milstd.py`, `tests/test_stanag4285.py` | Not covered |

## Packet-mode loss and interoperability results

Each row links to a committed lab note with date, configuration, and measured outcomes. These are historical
measurements from the listed setup, not guarantees for other paths or current benchmark runs.

| Mode / test | Recorded result | Evidence |
|---|---|---|
| AFSK 1200, offline synthetic impairments | At 2% RTP frame loss with repeat-last concealment: 27/40. | [AFSK baseline (2026-10-21)](../lab/AFSK_BASELINE_2026_10_21.md) |
| AFSK 1200, live ConfBridge without FEC | 3% simulated loss: 35/60 and 39/60; 5%: 25/60. | [ConfBridge milestone (2026-10-21)](../lab/CONFBRIDGE_MILESTONE1_2026_10_21.md) |
| FX.25, live ConfBridge at 3% loss | AFSK1200 plain 30/60 vs FX.25/32 58/60; G3RUH9600 plain 57/60, FX.25/32 55/60, FX.25/64 58/60. | [FX.25 report (2026-10-21)](../lab/FX25_2026_10_21.md) |
| Erasure-aware FX.25 and time diversity | Earlier G3RUH/PAM loss trials are in the linked note. A 2026-10-24 repeated two-station uplink-silence sweep delivered 40/40 with FX.25/32 at both tested levels; without FEC it delivered 36/40 at the moderate and 23/40 at the stronger level. | [Loss resilience report (2026-10-21)](../lab/LOSS_RESILIENCE_2026_10_21.md), [ConfBridge reliability report (2026-10-24)](../lab/CONFBRIDGE_RELIABILITY_2026_10_24.md) |
| Sender-side RTP packet omission | 2026-10-24: two-station FX.25/32 delivered 20/20 in both repeated trials; no-FEC delivered 18/20 and 16/20. Under a continuous third-talker masker, FX.25/32 recovered 10/10 zero-audio cases but 7/10 and 6/10 actual omitted-packet cases. | [RTP drop and masking report](../lab/ERASURE_PACKET_DROP_2026_10_24.md) |
| Speech masking and network-layer RTP drops | 2026-10-24: with a human-read speech sample as a third-talker masker, clean and every-40th-zeroed FX.25/32 runs delivered 10/10. A filtered 3% `tc netem` impairment delivered 5/10 and 7/10 without FEC, and 10/10 with FX.25/32 in both repeats; the qdisc measured 2.4–3.6% packet drops. | [Speech-masker and netem trials](../lab/SPEECH_MASK_NETEM_2026_10_24.md) |
| Randomized ConfBridge offered schedules | 2026-10-24: three matched seeds, three AFSK1200 stations, ten frames each; clean-channel totals were 174/180 without FEC and 166/180 with FX.25/32. Per-seed outcomes varied, and no FEC repairs were counted. This is a contention stress sample, not an FEC-under-loss result. | [Randomized schedule report](../lab/RANDOMIZED_SCHEDULE_2026_10_24.md) |
| Randomized offered schedules with RTP egress loss | 2026-10-24: two matched seeds, three stations, and filtered 3% station-0 RTP egress loss; measured rates were 1.8–3.5%. Totals were 104/120 without FEC and 108/120 with FX.25/32; receivers counted ten FX.25 repairs. This small, single-rate sample is not a general reliability estimate. | [Randomized loss report](../lab/RANDOMIZED_LOSS_2026_10_24.md) |
| Randomized RTP-loss rate sweep | 2026-10-24: at requested 1%, totals were 114/120 without FEC and 112/120 with FX.25/32; at requested 5%, 90/120 and 118/120. Each condition used two seeds and measured qdisc loss near its requested rate; sample size is small. | [Multi-rate randomized loss report](../lab/RANDOMIZED_LOSS_RATES_2026_10_24.md) |
| TNC-2 multiple streams and settings | Simulated three-node tests cover two outgoing or incoming links, peer-specific data, stream selection and Ctrl-\\. A live three-station ConfBridge check used the POSIX PTY command port to connect two peers on streams 0 and 1 and deliver distinct chat payloads to each. PTY command checks covered `MAXSTREAM 10`, listing/selecting stream 9, and `WRITE` response. JSON NVRAM round-trip includes `MAXSTREAM`; `tests.test_tnc2` passed 15 tests. | [Multi-stream unit lab note](../lab/TNC2_MULTISTREAM_2026_10_24.md), [PTY and TCP interface note](../lab/TNC2_INTERFACES_2026_10_24.md), [`tests/test_tnc2.py`](../tests/test_tnc2.py) |
| Third-party KISS-TCP exchange | Direwolf 1.7 joined the lab audio hub and `kissutil` connected to ham-modem KISS-TCP. `kissutil` transmitted a frame, Direwolf digipeated it, and `kissutil` received the digipeated frame; ham-modem also decoded Direwolf's beacon. | [PTY and TCP interface note](../lab/TNC2_INTERFACES_2026_10_24.md) |
| LinBPQ and JNOS over KISS-TCP | LinBPQ 6.0.26.1 connected to the KISS-TCP listener directly; JNOS 2.0q used a serial PTY bridge. Each established a bidirectional AX.25 connected chat with a TNC-2 PTY peer through the live SIP/PCMU ConfBridge channel. BBS application sessions and forwarding were not tested. | [Container interop report](../lab/LINBPQ_JNOS_INTEROP_2026_10_24.md), [container setup](../lab/interop/README.md) |
| LinBPQ and JNOS BBS applications | 2026-10-24: JNOS AX.25 mailbox and LinBPQ BPQMail sessions passed over separate ham-modem stations in the same ConfBridge. Bidirectional FBB passed manually and on the JNOS timer; reverse mail was read from the private mailbox, and the JNOS named spool volume retained it through container recreation. | [BBS interop report](../lab/BBS_INTEROP_2026_10_24.md), [FBB forwarding report](../lab/JNOS_FBB_FORWARD_2026_10_24.md) |
| G3RUH9600 live ConfBridge, room 7316 | 48/48 at 16 kHz with TXDELAY 100 ms. | [G3RUH 9600 report (2026-10-21)](../lab/G3RUH_9600_2026_10_21.md) |
| CSMA/CD, live multi-station room | Five stations x six frames: 99/120 plain vs 120/120 with CSMA/CD in run 1; 120/120 in both modes in run 2. | [ConfBridge milestone (2026-10-21)](../lab/CONFBRIDGE_MILESTONE1_2026_10_21.md) |

These sources preserve test setup and outcomes, but not every raw packet capture or harness log. New tests should
commit the command/configuration and summary alongside the lab note.

## Erasure handling in receive paths

The receive code now carries concealed-audio erasures into IL2P decoding for AFSK, G3RUH, and PSK, and into FX.25
decoding for AFSK, G3RUH, PAM, PSK, and QAM. FX.25 tag search ignores erased tag bits but requires at least 32
known tag bits; IL2P sync search requires 16 known sync bits. The TNC also marks near-silent blocks as erased for
80 ms after receive energy, covering near-total station-to-bridge loss that the bridge presents without RTP gaps.

On 2026-10-24, the affected modem and codec regression suite passed **81 tests**, and the full repository suite
passed **643 tests**. Exact commands, synthetic cases, and limitations are recorded in the [erasure recovery lab
note](../lab/ERASURE_RECOVERY_2026_10_24.md).
The live ConfBridge harness now supports `--tx-silence-every N`, which zeros selected non-silent RTP audio blocks
while preserving packet flow, and logs inferred silence-erasure counts at receivers. A repeated 2026-10-24 sweep
with 10 frames per station and FX.25 off/on delivered 20/20 clean frames in all runs; at every-40th active block
silence, no-FEC was 18/20 in both runs and FX.25/32 was 20/20 in both; at every-20th, no-FEC was 11/20 and 12/20,
while FX.25/32 was 20/20 in both. Impaired FX.25 runs repaired 9–10 frames, with zero bad FCS or RTP concealment.
This controlled two-station impairment is not an empirical network-loss distribution. A separate three-station
experiment with a continuous tone confirmed that a third audio source can mask silence inference. A repeated test
with a human-read speech sample still counted 28–31 inferred silence blocks per trial, including clean runs; those
whole-trial counters cannot attribute inference to individual damaged frames. A filtered 3% Linux egress qdisc
reduced no-FEC delivery to 5/10 and 7/10, while FX.25/32 delivered 10/10 in both repeats. A small clean-channel
randomized offered-load run showed mixed per-seed results. Filtered 1%, 3%, and 5% RTP-loss matrices with two
seeds are recorded; the 5% runs showed a larger FX.25/32 gain than the 1% or 3% runs ([randomized schedule
report](../lab/RANDOMIZED_SCHEDULE_2026_10_24.md), [3% loss report](../lab/RANDOMIZED_LOSS_2026_10_24.md),
[1% and 5% follow-up](../lab/RANDOMIZED_LOSS_RATES_2026_10_24.md)). More seeds, additional speech samples, and a
remote physical path remain open. Full results and logs
are in the [speech-masker and network-loss report](../lab/SPEECH_MASK_NETEM_2026_10_24.md).

## HF channel test tools

- `hammodem/hfchannel.py` implements Watterson/CCIR channel models.
- `tools/audio_hub.py --hf PRESET --snr DB` applies a channel to live audio paths.
- `tools/hf_bench.py` runs modem/channel/SNR sweeps.
- Historical results and configuration are described in [the HF channel lab note](../lab/HF_CHANNEL_2026_10_22.md).
- The mode-by-mode evidence review and fresh 2026-10-24 packet-modem sweeps are in the [HF mode audit](../lab/HF_MODE_AUDIT_2026_10_24.md).
  It records which modes have actual simulated fading/multipath evidence and which only have functional/AWGN tests.

## Interoperability scope

- Direwolf interoperability claims are recorded in the corresponding dated notes under `lab/` and summarized in
  [README](../README.md).
- The PACTOR-II/III and Clover-II implementations are experimental local modes. SCS/HAL hardware interoperability
  is unvalidated.
- Claims of current passing tests or live exchange should include a committed log or CI artifact with the command,
  date, configuration, and measured result.
