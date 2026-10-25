# ARQ failure-path regressions — 2026-10-24

## Coverage

The local HF link regression suite now covers three deterministic failure paths:

- **Lost data:** PACTOR-II and PACTOR-III recover when the initial uplink data burst is dropped; the sender retries
  and the receiver delivers the payload.
- **Lost ACK / duplicate data:** the lost-ACK loop test verifies each mode retries and ultimately completes. The
  receive-sequence test feeds repeated copies of the same sequence number and verifies every copy is ACKed while
  the application receives the payload only once.
- **Retry exhaustion:** with `max_retries=1` and all data uplink bursts dropped, both modes return to `idle`, clear
  their pending frame, deliver no payload, and report `link failure (timeout)`.

These tests exercise the shared local burst-link implementation with the PACTOR-II/III audio modems. They do not
establish SCS/HAL wire compatibility or performance over a live VoIP path.

## Reproduction and results

Targeted command:

```sh
.venv/bin/python -m unittest tests.test_hf_links tests.test_pactor_link tests.test_pactor2 tests.test_pactor3 tests.test_clover2
```

2026-10-24: **172 tests passed** in 7.363 seconds.

Full repository command:

```sh
.venv/bin/python -m unittest discover -s tests -t .
```

2026-10-24: **645 tests passed** in 67.720 seconds. This includes the retry-exhaustion regression and the RTP
silence-injection checks. Earlier full-suite counts are superseded; the latest record is also in
`docs/TESTING_VALIDATION.md`.

## Live ConfBridge silence-erasure measurement

The lab harness now has `--tx-silence-every N`. On station 0 it replaces every Nth non-silent outgoing audio block
with zero PCM while sending the RTP packet normally, preserving packet sequence and timestamp. Shutdown stats report
transmit-silenced blocks and inferred receive-side silence-erasure blocks. This is an application-level impairment
that models the bridge's downstream audio after uplink loss; it is not a literal packet drop on the network.

Commands from the repository root:

```sh
.venv/bin/python lab/confbridge_test.py --stations 2 --frames 4 --spacing 1 --fx25 32 \
  --room 7300@172.31.73.10 --base-port 18320
.venv/bin/python lab/confbridge_test.py --stations 2 --frames 4 --spacing 1 --fx25 32 \
  --tx-silence-every 20 --room 7300@172.31.73.10 --base-port 18330
```

On 2026-10-24, both runs delivered **8/8 frames (100%)**. In the impaired run, station 0 sent 9 silenced active
blocks; station 1 reported 21 inferred silence-erasure blocks, 2 FX.25 repairs, 0 bad FCS, 718 RTP packets received,
and 0 concealed samples. Station 0 reported 719 RTP packets sent. This confirms the injected silent payloads
arrived through the bridge as continuous RTP and activated erasure inference; it does not show a delivery-rate gain
because the clean baseline also delivered every frame and the sample was small. The simulator silences full 20 ms
audio blocks, which is a controlled lab impairment rather than a measured real network failure distribution.

The PBX was healthy during the run and the bridge was empty afterward. Other active talkers may mask a lost station
by keeping the mixed audio above the configured silence threshold; the two-station run does not evaluate that case.
