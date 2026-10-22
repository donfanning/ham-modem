# Phase 8: HF channel simulator — 2026-10-22

`hammodem/hfchannel.py`: streaming Watterson model (CCIR 520 / ITU-R F.1487). Two independent equal-power
Rayleigh paths on the analytic signal (127-tap Hilbert FIR), second path delayed; each path gain a unit-power
complex Gaussian process with a Gaussian Doppler spectrum (two-sigma width = spread), generated at a low rate
and interpolated (vectorised); AWGN referenced to 3 kHz; optional frequency offset and impulse noise.
Presets: awgn, good (0.5 ms, 0.1 Hz), moderate (1 ms, 0.5 Hz), poor (2 ms, 1 Hz), flutter (0.5 ms, 10 Hz),
F.1487 mid-latitude disturbed (2 ms, 1 Hz) and low-latitude disturbed (6 ms, 10 Hz).

Tools: `tools/hf_channel.py` (WAV in/out), `tools/audio_hub.py --hf PRESET --snr DB` (live: independent
fading per talker, receiver noise per listener), `tools/hf_bench.py` (modem x channel x SNR grid).

## Validation (`tests/test_hfchannel.py`)

Transparent (pure Hilbert-delay) with no fading/noise; Doppler processes unit power with the requested spread at
0.5 / 1 / 10 Hz; Rayleigh envelope (median sqrt(ln 2)); exact two-path delay; SNR in 3 kHz within 0.5 dB.
Bug caught by the spread test: the Gaussian filter width was set from the *amplitude* response; the Doppler
spectrum is the *power* response (narrower by sqrt 2): measured 0.36 Hz for 0.5 Hz before the fix.

## Baseline (20 x ~60-byte UI frames, 300 ms TXDELAY, offline)

| modem | channel | 25 dB | 20 | 15 | 10 | 5 |
|---|---|---|---|---|---|---|
| afsk300 | awgn | 20 | 20 | 20 | 20 | 20 |
| afsk300 | good | 18 | 18 | 17 | 14 | 11 |
| afsk300 | moderate | 10 | 11 | 7 | 5 | 4 |
| afsk300 | poor | 5 | 2 | 2 | 2 | 1 |
| afsk1200 | awgn | 20 | 20 | 20 | 20 | 2 |
| afsk1200 | good | 16 | 14 | 9 | 1 | 0 |
| afsk1200 | moderate | 9 | 8 | 6 | 5 | 0 |
| afsk1200 | poor | 7 | 7 | 5 | 5 | 0 |
| afsk300 + FX.25/32 | good | 18 | 18 | 18 (15 dB) | | 12 (5 dB) |
| afsk300 + FX.25/32 | moderate | 17 | 14 | 14 | 13 | 9 |
| afsk300 + FX.25/32 | poor | 5 | 4 | 4 | 2 | 1 |

The textbook picture: classic HF packet survives noise but collapses under fading; FEC helps on moderate
channels but not poor ones. This is the yardstick for phases 9-10 (PSK modes, IL2P 300, ARDOP).

Live: two afsk300 stations on `audio_hub.py --hf good --snr 20`, FX.25/32: 4/4 delivered (2 plain copies failed
FCS, 1 frame repaired by FX.25).

## Bug found: duplicate suppression vs. retransmissions

The FX.25 row first showed 40 of 20: at 300 bd the FX.25 copy decodes > 1 s after the embedded plain copy, past
AFSK's 1 s duplicate window. Widening the window to 16k bits fixed that but swallowed connected-mode SABM/DISC/RR
retries (byte-identical, seconds apart) -- three link tests failed. Duplicates are now dropped only within the
same burst (DCD rising edge to rising edge) or a short near-window; retransmissions always come in a new burst.
Regression test: `test_each_fx25_frame_delivered_once_at_300_bd`.
