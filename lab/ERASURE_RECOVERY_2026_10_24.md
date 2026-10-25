# Packet receive erasure recovery — 2026-10-24

## Changes covered

- Known erasure masks now flow into IL2P RS decoding from AFSK, G3RUH, and PSK demodulation. IL2P sync matching
  ignores marked bits while requiring at least 16 of 24 sync bits to remain known.
- FX.25 tag matching ignores marked bits, but rejects candidates with fewer than 32 known bits in the 64-bit tag.
- FX.25 erasure masks now reach PSK and QAM receive paths as well as the existing AFSK, G3RUH, and PAM paths.
- The packet TNC identifies near-silent blocks during an 80 ms receive-activity hangover as erasures. This covers
  uplink audio lost before the bridge mixer, which arrives downstream as ordinary silence rather than an RTP gap.

The silence detector uses an RMS threshold of -65 dBFS by default. It can identify near-total silence in a recent
receive burst; it cannot identify which station was lost if another talker keeps the mixed audio above threshold.
This test is an in-process receive-path regression, not an Asterisk network trial.

## Regression coverage

- AFSK, G3RUH, and PSK waveform tests confirm erased sample spans produce marked IL2P bits; QAM confirms the same
  propagation to FX.25.
- IL2P recovers a frame when six flipped sync bits are marked erased, and rejects sync windows with fewer than
  sixteen known bits. Existing payload tests verify RS recovery from nine marked bad bytes.
- FX.25 finds a tag with six marked bad bits and rejects a tag window with fewer than 32 known bits.
- A TNC receive test feeds a signal followed by silence: the first silent block within the configured hangover is
  marked erased and a later block is not.

## Reproduction and result

From the repository root:

```sh
.venv/bin/python -m unittest \
  tests.test_afsk \
  tests.test_g3ruh \
  tests.test_psk \
  tests.test_qam \
  tests.test_pam \
  tests.test_fx25 \
  tests.test_il2p \
  tests.test_tnc \
  tests.test_erasure_path
```

2026-10-24: **81 targeted tests passed** in 19.398 seconds. The suite covers the listed waveform propagation,
decoder recovery and silence-detection regressions, plus existing tests for each affected modem and codec.

The full repository suite also passed **643 tests** in 65.500 seconds:

```sh
.venv/bin/python -m unittest discover -s tests -t .
```

Neither run measures real bridge packet loss or recovery rates. The silence detector still needs a live ConfBridge
trial; another station's audio can mask a lost uplink by keeping the mix above threshold.
