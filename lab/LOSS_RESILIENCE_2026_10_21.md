# Loss resilience for the fast modes: erasure-aware FX.25 and time diversity — 2026-10-21

A lost 20 ms RTP packet destroys baud x 20 ms / 8 bytes: 3 at 1200, 24 at 9600, 48 at 19200, 144 at 57.6k.

## 1. Erasure-aware FX.25 (receiver only; still Direwolf-compatible on the air)

The RTP layer knows which samples it concealed (`ConcealedPcm`). Demodulators keep those spans on their own
sample clock (widened by their filter length), flag bits decided inside them, and carry the flag forward over
NRZI (+1 bit) and the G3RUH/PAM descrambler (+17 bits). Any byte holding a flagged bit becomes an RS erasure.
Errors-and-erasures RS decoding (Berlekamp-Massey seeded with the erasure locator) fixes v errors + e erasures
whenever 2v + e <= nroots, so an RS/32 code repairs a whole lost packet at 9600 (24 bytes) instead of 16 bytes.

Offline, 3% loss with repeat-last concealment, 120 frames of ~175 bytes per row:

| modem / FX.25 | errors only | with erasures |
|---|---|---|
| afsk1200 / 16 | 93 | 100 |
| g3ruh9600 / none | 96 | 96 |
| g3ruh9600 / 32 | 107 | 116 |
| g3ruh9600 / 64 | 116 | 118 |
| g3ruh19200 / 64 | 109 | 117 |
| pam57600 / 64 | 113 | 113 (a lost packet is 144 bytes: beyond any FX.25 code) |

Live, lab rooms, 3 stations x 10 frames, 3% simulated loss, CSMA/CD, single runs:
g3ruh9600 FX.25/32: 55/60 (2 repairs) before erasures -> **59/60 (12 repairs)**; g3ruh19200 FX.25/64: 60/60.

## 2. Time diversity for 57.6k: `--repeat N`

Each burst sends its frames N times as whole rounds separated by 40 ms of flags (one carrier, so the far end
keeps clock/scrambler lock); receivers already drop duplicates. A single lost packet can never hit both copies:
`tests/test_diversity.py` loses packets across the whole burst one at a time and both frames always arrive (and
with one copy, a mid-burst loss loses the frame). Cost: a second copy of a 256-byte frame is ~36 ms of air.

Live, room 7348, pam57600, 3 stations x 10 frames, **5%** simulated loss, CSMA/CD, single runs:
`--repeat 1` 51/60 (85%) -> **`--repeat 2` 60/60 (100%)**.

## Limits

- Simulated loss is on the bridge -> station leg, where the receiver sees the RTP gap. Loss on the station ->
  bridge leg reaches *everyone* as silence inside the mix with no gap to flag: erasure marking cannot help there,
  `--repeat` still does.
- Plain AX.25 receivers (e.g. Direwolf KISS clients) see `--repeat` copies as duplicates; APRS software dedupes,
  but leave it off on mixed channels.
- 1200 bd FX.25/16 with erasures still lost 20/120: losses hitting the 64-bit tag (> 8 bad bits) are not
  recoverable; an erasure-tolerant tag search would be the next step.
