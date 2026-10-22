# Phase 11.0: codec2 OFDM data modes (FreeDATA waveforms) — 2026-10-22

`hammodem/freedata/codec2.py` wraps libcodec2's raw-data modes with ctypes (libcodec2 is LGPL-2.1, loaded
dynamically, nothing copied; `HAM_MODEM_CODEC2_LIB` selects a specific library). It adds the CRC16 the raw API
expects and exposes `Datac(mode, frames_per_burst).modulate([payloads])` / `.feed(samples)`.

| mode | payload bytes / frame | one-frame burst | available in Ubuntu libcodec2 1.2.0 |
|---|---|---|---|
| datac0 | 14 | 1.5 s | yes |
| datac13 | 14 | 6.2 s | yes |
| datac4 | 54 | 5.2 s | yes |
| datac3 | 126 | 3.3 s | yes |
| datac1 | 510 | 4.3 s | yes |
| datac14 | 3 | 1.6 s | no (needs a newer build; master builds with cmake, tested) |

## Gotchas found

* The receiver must be told the burst length: `freedv_set_frames_per_burst(f, n)`. With the default, datac13 never
  decoded and datac4 missed bursts at some alignments (found by comparing with codec2's own `freedv_data_raw_tx/rx`,
  which set it). With it set, every mode decodes at every start offset tried.
* Calling `freedv_get_modem_sample_rate` without declared argtypes crashes the interpreter (pointer truncation);
  all calls are declared.
* Samples are 8 kHz, so every mode fits G.711 directly; all pass µ-law in tests/test_freedata_codec2.py.

## Performance (lab/freedata/sweep.py, hfchannel, 10 single-frame bursts per point; ~5 min on one core)

SNR (dB in 3 kHz) at which every one of 10 frames decoded / at which at least half did:

| channel | datac0 | datac1 | datac3 | datac4 | datac13 |
|---|---|---|---|---|---|
| AWGN | 0 / 0 | 4 / 4 | -2 / -2 | -6 / -8 | -8 / <-8 |
| moderate (1 ms, 0.5 Hz) | 12 / 2 | 10 / 6 | 12 / 2 | 10 / -4 | 8 / -6 |
| poor (2 ms, 1 Hz) | 6 / 4 | 8 / 6 | 4 / 0 | 4 / -4 | 2 / -6 |

(datac1 carries 510 B per frame and needs the most SNR; datac13/4 are the narrow robust modes. Fading with only a
single frame and no repeat is the hard case; FreeDATA's ARQ layer covers it by repeating.)

## 11.1 TNC modems `--modem datac0|datac1|datac3|datac4|datac13` (hammodem/modems/datac.py)

AX.25 frames ride the datac chunks as our own segmentation (not FreeDATA's protocol): a 2-byte chunk header (continuation
bit + chunk index mod 128; burst size and position), then length-prefixed AX.25 records; small frames share a chunk,
long ones span chunks, a lost chunk costs only the frames it touches. Bursts hold at most ~12 s of chunks. Everything
else (KISS, TNC-2, AGWPE, AX.25 connected mode, CSMA) works unchanged on top; `--repeat N` sends the transmission N
times and the receiver drops repeats inside one transmission (1.5 s of silence ends it, so a later link-layer
retransmission still gets through).

Receiver quirk handled in the modem: codec2's burst receiver stays synchronised until it has seen the number of
chunks it was told to expect, so a shorter burst would hide the next one (found live: a 40 B frame, then a 300 B frame
from the same station was never heard). The chunk header tells the receiver when a burst is over, with a deadline
as fallback; tests/test_datac_modem.py covers it.

Lab (lab/freedata/confbridge_datac_test.py, two ham-modem processes in room 7300, SIP G.711, KISS-TCP hosts):

| mode | 40 B info frame | 300 B info frame |
|---|---|---|
| datac3 | intact both ways, ~3.7 s | intact both ways, 10-11 s (1 of 3 runs needed `--repeat 2`: 20 s) |
| datac4 | intact both ways, ~11 s | frequently lost without repeats (about half the runs); 38 s when it arrives |

The datac4 losses are real VoIP-path effects, not the modem: capturing the received audio (`DATAC_LOG=prefix`) and
correlating with the transmitted waveform shows the signal matching to the sample for the first ~27 s and then
slipping by tens of samples (the PBX re-timing its mix); a narrow, slow OFDM frame cannot track a jump like that, and
each chunk it hits is lost. Shorter bursts, `--repeat`, and the AX.25 layer's retransmissions limit the damage; the
faster modes (datac3, datac1, datac0) finish a frame before the path misbehaves. Real fix, later: chunk-level
combining across repeats.
