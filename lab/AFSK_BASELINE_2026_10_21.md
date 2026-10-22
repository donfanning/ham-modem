# AFSK modem baseline and Direwolf interop — 2026-10-21

First offline qualification of `hammodem/modems/afsk.py` (correlator + 5-slicer twist bank at 1200 bd,
3 slicers at 300 bd, PLL inertia 0.74/0.50, DCD from transition-timing quality).

## Interop (Direwolf from Debian bookworm `direwolf` package, `atest` / `gen_packets`)

| Direction | 1200 bd | 300 bd |
|---|---|---|
| ours `afsk_wav.py gen` -> Direwolf `atest` | 50/50 | 20/20 |
| Direwolf `gen_packets -n` -> ours `afsk_wav.py decode` | see below | see below |

`gen_packets -n N` adds increasing noise across the run, so it is also a sensitivity ramp:

| File | ours | Direwolf atest (default profile) |
|---|---|---|
| `gen_packets -B 1200 -r 8000 -n 100` | 36 | 23 |
| `gen_packets -B 1200 -r 44100 -n 100` | 79 | 67 |
| `gen_packets -B 300 -r 8000 -n 50` | 14 | 14 |

CPU: 1.0% of one core at 8 kHz (1200 bd), 3.1% at 44.1 kHz — far inside the 20 ms RTP budget that bit
El-Modem's V.34 receiver.

## Parameter sweep (40 frames per cell, 8 kHz, synthetic impairments in `tests/impair.py`)

1200 bd: correlator window 1.2 bit, post-detector low-pass 1.25 x baud chosen. Results at that setting:
AWGN 7000 rms on a 16384-peak signal 17/40; de-emphasis or pre-emphasis + 4000 rms 39-40/40;
+500 ppm clock drift + 4000 rms 39/40; 2% RTP frame loss (repeat-last concealment) 27/40.

300 bd: window 1.25 bit, low-pass 0.75 x baud: AWGN 12000 rms 36/40; +15 Hz / -30 Hz SSB mistuning 40/40;
+500 ppm 40/40.

## Takeaways

- Packet loss, not noise, is the VoIP channel's real enemy: an 85-byte frame with 300 ms TXDELAY spans ~43
  RTP frames, so at 2% loss only ~42% would survive untouched; repeat-last concealment lifts that to ~67%.
  Short TXDELAY, short PACLEN and later FX.25/IL2P matter more than demodulator tuning.
- The WA8LMF TNC Test CD track 2 benchmark is still to be run.
