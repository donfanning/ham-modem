# HF mode testing revalidation and evidence audit — 2026-10-24

## Scope

Rechecked the current test suite and the HF-mode claims after comparing them with adaptive HF modem capabilities.
This audit distinguishes codec/PHY round-trips, AWGN tests, simulated ionospheric channels, VoIP/software-peer
interoperability, and real over-the-air interoperability. None of the results below is a real-radio HF test.

## Current repository test suite

Command:

```sh
.venv/bin/python -m unittest discover -s tests -t .
```

Result on 2026-10-24: **652 tests passed** in 66.974 s. This verifies the current software test cases; the suite is
not a single performance or RF interoperability test. In particular, a noisy AWGN unit test does not establish
performance on a frequency-selective, time-varying HF path.

## Repeated HF simulator sweep

Repeated the existing packet benchmark at a new fixed seed. Each cell is delivered UI frames out of 20, each about
60 bytes, with 300 ms TX delay. SNR is measured in 3 kHz by the simulator. These are single-seed synthetic-channel
results, not confidence intervals or deployed-link throughput.

Command without FX.25:

```sh
.venv/bin/python tools/hf_bench.py --modems afsk300,afsk1200,psk2400 --presets awgn,good,moderate,poor --snr 25,20,15,10 --frames 20 --seed 17
```

| modem | channel | 25 dB | 20 dB | 15 dB | 10 dB |
|---|---|---:|---:|---:|---:|
| AFSK 300 | AWGN | 20 | 20 | 20 | 20 |
| AFSK 300 | good | 20 | 19 | 19 | 18 |
| AFSK 300 | moderate | 14 | 13 | 13 | 13 |
| AFSK 300 | poor | 6 | 6 | 5 | 3 |
| AFSK 1200 | AWGN | 20 | 20 | 20 | 20 |
| AFSK 1200 | good | 14 | 14 | 11 | 4 |
| AFSK 1200 | moderate | 9 | 9 | 4 | 0 |
| AFSK 1200 | poor | 6 | 6 | 3 | 3 |
| PSK 2400 | AWGN | 20 | 20 | 20 | 11 |
| PSK 2400 | good | 3 | 2 | 0 | 0 |
| PSK 2400 | moderate | 0 | 0 | 0 | 0 |
| PSK 2400 | poor | 2 | 2 | 0 | 0 |

Command with FX.25/32:

```sh
.venv/bin/python tools/hf_bench.py --modems afsk300,afsk1200 --presets good,moderate,poor --snr 20,15,10,5 --frames 20 --fx25 32 --seed 17
```

| modem | channel | 20 dB | 15 dB | 10 dB | 5 dB |
|---|---|---:|---:|---:|---:|
| AFSK 300 + FX.25/32 | good | 18 | 18 | 17 | 13 |
| AFSK 300 + FX.25/32 | moderate | 14 | 13 | 11 | 8 |
| AFSK 300 + FX.25/32 | poor | 7 | 7 | 3 | 2 |
| AFSK 1200 + FX.25/32 | good | 15 | 11 | 8 | 0 |
| AFSK 1200 + FX.25/32 | moderate | 6 | 4 | 1 | 0 |
| AFSK 1200 + FX.25/32 | poor | 4 | 4 | 1 | 0 |

Interpretation: low-rate AFSK benefits from longer symbol time on these modeled multipath channels, while its packet
delivery still falls sharply in poor fading. FX.25/32 is not an adaptive HF modem; the new single-seed results show
mixed outcomes and do not support a blanket claim that it improves HF delivery. The 1200-baud PSK mode fails in
the good/moderate/poor multipath presets despite passing AWGN, so this implementation is not suitable as an HF
waveform without equalization or a more HF-robust design.

## Per-mode evidence audit

| Mode family | Evidence actually available | Revalidation conclusion / gap |
|---|---|---|
| AFSK 300/1200 | Repeated HF simulator packet sweep; prior live ConfBridge tests; AX.25/HDLC tests | Current code passes. These are non-adaptive baselines; 300 baud is more tolerant in the current simulator, but neither is a high-rate HF design. |
| Direwolf PSK 2400/4800 | AWGN and Direwolf audio-file interop; PSK2400 HF simulator sweep; 4800 is unsuitable for SSB mistuning and HF multipath | PSK2400 is not an HF channel solution in its current single-carrier/no-equalizer form. 4800 is a VHF/FM/VoIP-oriented mode here, not a candidate for ordinary HF SSB. |
| ARDOP | PHY compatibility with ardopcf recordings; live software-peer/Pat/ConfBridge interop; moderate 15 dB and poor 10 dB simulated HF ARQ runs with intact payload and gear shifts | Strongest open adaptive HF candidate in this repository. Still no physical RF, and its existing channel sample is too small to characterize throughput or adaptation thresholds. See [PHY](ARDOP_PHY_2026_10_22.md), [ARQ](ARDOP_ARQ_2026_10_22.md), and [host](ARDOP_HOST_PAT_2026_10_22.md) reports. |
| FreeDATA codec2 datac modes | Ten single-frame bursts per SNR point across AWGN/moderate/poor; separate ConfBridge tests | Useful HF channel evidence for raw waveforms, but single-frame CRC success is not ARQ goodput. The project wrapper's repeats are not the FreeDATA ARQ/session protocol; that layer and chunk combining remain open. See [FreeDATA report](FREEDATA_CODEC2_2026_10_22.md). |
| AMTOR/SITOR | ITU code/protocol tests; one moderate HF-channel case at 14 dB; local UDP and ConfBridge exchanges | Some HF impairment coverage, but no parameter sweep or real AMTOR peer interoperability. |
| PACTOR-I | Audio/codec, AWGN/noise, and local ARQ loop tests | No Watterson/CCIR channel sweep or live ConfBridge/real PACTOR peer result. Protocol coverage follows public descriptions/source; wire interop remains unverified. |
| Local PACTOR-II/III and Clover-II | Frame/codec tests, Gaussian-noise cases, link/ARQ regression and some live ConfBridge results for local PACTOR-II/III | Does not validate ionospheric fading, SCS/HAL interoperability, or marketed PACTOR-II/III performance. Clover-II has less link evidence still. Keep their implementation status explicitly experimental. |
| MIL-STD-188-110 / STANAG 4285 | Published-waveform round-trips, selected AWGN tests, and rate/profile tests | No current HF simulator sweep or independent over-air peer test is documented. Despite their HF design origins, our test results do not yet demonstrate HF-channel performance. |
| PSK31/63, RTTY, MFSK16, Olivia/Contestia, DominoEX/THOR, JS8 | Mode-specific round-trips and selected AWGN tests; one historical Asterisk keyboard capture was unsuccessful/incomplete | No channel-selective HF simulator sweep and no verified live keyboard exchange in the recorded capture. These pass functional tests but do not yet have mode-comparative HF robustness results. |
| High-rate G3RUH, PAM/QAM and other VoIP-native packet modes | Synthetic noise/loss and VoIP/ConfBridge tests at their configured sample rates | These prove VoIP/audio-leg behavior only. Their wider bandwidth and/or timing assumptions make them unsuitable to infer stock HF SSB performance. |

## Overall assessment

The project has broad mode implementation and functional coverage, but **HF-adaptation performance coverage is narrow**.
The evidence supports ARDOP as the most developed open adaptive HF path and FreeDATA datac as a tested set of HF
waveforms; it does not support applying their rates or robustness to every implemented mode. The generic Shannon
capacity calculation remains an idealized ceiling, not a project rate estimate. Published PACTOR-III/IV figures
are external product/protocol capabilities and must not be attributed to our local PACTOR-II/III experiments.

Next measurement priority: use the same seeded channel/SNR matrix and multiple independent seeds for ARDOP and
FreeDATA, report delivered payload bytes per elapsed wall time (including preambles, turnarounds, retries, and
control frames), and add channel sweeps for AMTOR, the local ARQ links, MIL-STD/STANAG, and keyboard modes only
where they are intended to operate on HF. Follow simulated evidence with authorized over-the-air tests when
hardware and a peer are available.
