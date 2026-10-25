# ARQ retry and memory-combining regression — 2026-10-24

## Scope

Regression coverage for the local PACTOR-II/III ARQ links. This is a deterministic in-process audio-loopback test,
not an Asterisk or radio interoperability run.

The test drops B's first reply after A sends a data burst (ticks 50 through 59 in the 800-sample test clock).
This models a lost ACK: A times out and retransmits its sequence-0 frame. B recognizes the repeated sequence,
ACKs it again, and delivers `HELLO` to its application only once. The same scenario runs against PACTOR-II and
PACTOR-III.

Separate focused checks verify that a successful PACTOR-II/III data decode resets the memory-ARQ soft-bit
accumulator, while an unsuccessful decode retains accumulated bits for a retransmission attempt. A direct receive
sequence check covers ACKing duplicate frames and NACKing a corrupted frame with the expected receive sequence.

## Reproduction

From the repository root, with the project virtual environment installed:

```sh
.venv/bin/python -m unittest \
  tests.test_pactor_link \
  tests.test_pactor2 \
  tests.test_pactor3 \
  tests.test_hf_links \
  tests.test_clover2
```

## Result

2026-10-24: **171 tests passed** in 5.995 seconds. This includes the new lost-ACK retry case for both PACTOR-II
and PACTOR-III, the focused soft-combining reset/retention cases, existing protocol unit tests, and two-station
link tests. The result covers the deterministic local harness only; it does not establish performance over RTP,
ConfBridge, or SCS/HAL hardware.

Regression tests: [`tests/test_pactor_link.py`](../tests/test_pactor_link.py) and the lost-ACK case in
[`tests/test_hf_links.py`](../tests/test_hf_links.py).
