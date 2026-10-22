# PACTOR Reverse-Engineering Research — 2026-10-22

## Approach

Clean-room, black-box. Sources: published papers, SCS technical descriptions,
Wavecom decoder documentation, ARRL summaries, the GPL `hf`/hfterm PACTOR-I
implementation (Thomas Sailer, ETH), and the `pdn-soundmodem` project's signal
measurements. No vendor firmware or binaries were disassembled.

---

## PACTOR-I (confirmed from multiple sources + hfterm source)

### Modulation
- FSK, 200 Hz shift, 100 or 200 baud.
- Bandwidth: < 600 Hz (receivable through a 500 Hz filter).

### Frame structure

| Field | 100 bd | 200 bd | Notes |
|---|---|---|---|
| Header | 1 byte | 1 byte | 0x55 or 0xAA (alternates each new packet) |
| Data | 8 bytes | 20 bytes | Huffman or plain ASCII |
| Status | 1 byte | 1 byte | See below |
| CRC | 2 bytes | 2 bytes | CRC-CCITT, see below |
| **Total** | **12 bytes (96 bits)** | **24 bytes (192 bits)** | |

### Timing
- Full cycle: 1.25 s (long path: 1.40 s).
- Packet duration: 0.96 s.
- CS slot: 0.12 s (12 bits at 100 bd).
- Idle/turnaround: 0.29 s (0.17 s propagation + settling).

### Status byte (from hfterm `pactor.c`)

| Bits | Meaning |
|---|---|
| 1:0 | Packet counter (modulo 4) |
| 3:2 | Compression mode: 0 = plain ASCII, 1 = Huffman |
| 6 | Break-in request (0x40) |
| 7 | QRT flag (0x80) |

Bits 4–5 are unused / reserved in PACTOR-I.

### CRC

**Polynomial:** CRC-CCITT, x^16 + x^12 + x^5 + 1.
**Table:** Reflected (LSB-first), table[1] = 0x1189. This is the same as HDLC/X.25.
**Init value:** 0xFFFF (from `pp.crc_preset`, default `{0xffff, 0xffff, 0xffff, 0xffff}`).
**Final XOR:** Inverted (`~crc & 0xffff`).
**Covers:** Data + status byte (everything except the header).
**Byte order:** CRC is sent LSB first (low byte, then high byte).
**Expected good CRC:** `PACTOR_CORRECT_FCS = 0xf47`.

The CRC preset can differ per packet length (10, 11, 21, 23 bytes),
but the default is 0xFFFF for all four. Some implementations use
non-standard presets (the `pactor_set_params` function accepts them).

### Control signals (12 bits each, at 100 baud)

| CS | Hex | Binary | Function |
|---|---|---|---|
| CS1 | 0x4D5 | 010011010101 | Acknowledge |
| CS2 | 0xAB2 | 101010110010 | Busy / reject |
| CS3 | 0x34B | 001101001011 | Break-in (changeover) |
| CS4 | 0xD2C | 110100101100 | Speed change |

Inverted forms: `0xFFF ^ CSn` (CS1 inv = 0xB2A, etc.)
Minimum Hamming distance: 8 bits.

### Huffman table

The full 128-entry Huffman table is in `pactor.c` lines 174–208.
Each entry has `{len, code}` where the code is LSB-first.
Character 0x1E is the idle code. Codes 0–31 are control characters
(mostly 15-bit). Printable ASCII (32–127) has shorter codes.
Average ~4 bits/char for English text.

### Memory ARQ
- Soft samples from the FSK demodulator (8-bit ADC) are accumulated
  across retransmissions of the same packet.
- The accumulated values are hard-decided and tested against the CRC.
- Shift polarity alternates every transmission to average out constant
  interferers.

### Connect sequence
1. Master sends sync packet: header (0x55, 100 bd) + slave callsign
   (8 bytes, padded with 0x0F), repeated in 100 bd and 200 bd.
2. Slave replies CS1 (200 bd OK) or CS4 (fall back to 100 bd).
3. CS2 = busy.
4. First data packet: header 0xAA, counter = 1, contains master callsign.

### QRT
- QRT packet: status bit 7 set, data field contains receiver's callsign
  in reverse byte order + header byte, at the current speed.
- 200 bd QRT: callsign bytes are doubled (each nibble → byte via `dbl_tab`).

---

## PACTOR-II (from Helfert/Rink DL6MAA/DL2FAK, ARRL, Wavecom, pdn-soundmodem measurements)

### What is publicly known

#### Modulation
- Two-tone DPSK (differential phase-shift keying).
- Tone spacing: 200 Hz.
- Symbol rate: 100 baud per tone (confirmed by measurement: pdn-soundmodem).
- Raised-cosine pulse shaping.
- Crest factor: 1.45.
- Center frequency: adjustable in 1 Hz steps (400–2600 Hz), keeping 200 Hz shift.
- Bandwidth: ~450 Hz at −50 dB.
- Tone swapping: information alternates between the two tones every cycle,
  so a narrowband interferer on one tone reduces speed but doesn't kill the link.
- Sideband: USB or LSB.
- Frequency tracking: ±100 Hz automatic; ±80 Hz for initial setup.

#### Speed levels

| Level | Modulation | Code rate | Raw (bps) | Net (bps) | Bytes/pkt (1.25 s) | Bytes/pkt (3.75 s) |
|---|---|---|---|---|---|---|
| 1 | DBPSK | 1/2 | 200 | 100 | 5 | 36 |
| 2 | DQPSK | 1/2 | 400 | 200 | 14 | 76 |
| 3 | 8-DPSK | 2/3 | 600 | 400 | 32 | 156 |
| 4 | 16-DPSK | 7/8 | 800 | 700 | 59 | 276 |

Maximum throughput: 700 bps uncompressed, ~1200 bps with PMC compression.

#### Frame structure
- Fields: **header** (8 pulses), **variable data**, **status byte**, **CRC**.
- Standard cycle: 1.25 s, standard packet: 0.8 s.
- Data mode (long cycle): 3.75 s cycle, 3.28 s packet, up to 2208 information bits.
- Long-cycle is requested via a flag in the status byte; accepted by CS6.

#### Timing (confirmed by pdn-soundmodem measurement on a live P-II signal)
- ARQ cycle: 3.7500 s (σ = 2.4 ms) in data mode.
- Data from ISS: 3.30–3.33 s.
- Gap: 45 ms.
- CS from IRS: 0.255–0.275 s.
- Gap: 135 ms.
- Non-data overhead: 0.445 s (spec says 0.45 s).

#### Control signals
- Six 40-bit codes (not 12-bit like PACTOR-I).
- Mutual Hamming distance: 24 bits (Plotkin bound).
- All CS sent as DBPSK for robustness.
- Detection: cross-correlation using DSP analog detail + memory ARQ buffer.

| CS | Function |
|---|---|
| CS1, CS2 | Acknowledge / request packets |
| CS3 | Break-in (direction change) |
| CS4, CS5 | Speed level changes |
| CS6 | Toggle packet length (accept long-cycle) |

#### Error-correction coding
- **Base code:** Rate 1/2 convolutional, constraint length K = 9 (8 shift registers).
- **Higher rates:** 2/3 and 7/8 by puncturing. Erased positions filled with midpoint
  neutral value before Viterbi decoding.
- **Decoding:** Soft-decision Viterbi (maximum likelihood).
- **Interleaving:** Full frame interleaving (details unknown).
- **Memory ARQ:** Analog memory ARQ combined with coding at all speed levels. Hybrid ARQ.

**Generator polynomials:** NOT PUBLISHED by SCS. However, the "optimal" rate-1/2
K=9 convolutional code is well-known in coding theory: generators (561, 753) octal
= (101110001, 111101011) binary. This is used in IS-95 CDMA and many other systems.
It is the most likely candidate, but this is an inference, not a confirmed fact.

#### CRC
- 16-bit CCITT (same polynomial as PACTOR-I: x^16 + x^12 + x^5 + 1).
- Status byte and CRC bytes are included in the interleaved/coded block.
- 8 flushing bits follow the CRC to flush the convolutional encoder tail.

#### Status byte (PACTOR-II extensions over PACTOR-I)
- Bits 1:0 — modulo-4 packet number (same as P-I).
- Bits for language/compression mode.
- Bits 5–7 — cycle-length suggestion, changeover request, QRT.
- Exact bit positions: not publicly documented.

#### Compression
- Huffman (separate upper/lower case tables).
- Run-length encoding.
- Pseudo-Markov Compression (PMC): Markov coding on 16 most frequent
  preceding characters. Separate English and German tables.
- Automatic selection among 6 methods (including plain ASCII).
- Binary data: compression auto-disabled, 4-bit nibble packing.
- Average factor: ~1.9 for PMC on text.

#### Link setup
- Always starts as PACTOR-I (FSK, 100/200 bd).
- Both stations must have MYLEVEL ≥ 2 to upgrade to PACTOR-II.
- MYLEVEL = 1 keeps the link in FSK PACTOR-I.

#### Listen mode
- Monitors all PACTOR-I and PACTOR-II modulation forms simultaneously.
- Only the short header must decode correctly; the rest benefits from FEC.

### What is NOT publicly known (the gaps)

1. **Convolutional code generator polynomials** — likely (561, 753) octal but unconfirmed.
2. **Puncture patterns** for rates 2/3 and 7/8.
3. **Interleaver structure** — "full frame" but dimensions/algorithm unknown.
4. **Scrambler polynomial** (if any).
5. **Header format** — described as "8 pulses" supporting QRG-tracking, listen mode,
   and memory ARQ. Exact bit/symbol layout unknown.
6. **Status byte bit layout** — approximate fields known, exact positions not.
7. **40-bit control signal codes** — the six CS values themselves are not published.
8. **Memory ARQ combining algorithm** — analog accumulation is described but the
   exact weighting/threshold is not.
9. **PMC Huffman tables** — not published (the P-I Huffman table IS public; see above).
10. **Differential encoding details** — phase mapping for DQPSK/8DPSK/16DPSK
    (Gray coding? Natural?) not specified.

---

## PACTOR-III (brief summary from Wavecom)

- Multi-channel PSK, pulse-shaped OFDM, up to 18 carriers.
- Carrier frequency: 480 + 120 × n Hz (n = 0..17).
- Symbol rate: 100 baud per carrier.
- Bandwidth: 2.4 kHz.
- 6 speed levels, convolutional code K = 7 or 9, rates 1/2 to 8/9.
- Up to 3600 bps raw, 3200 bps user data.
- Setup starts as PACTOR-I, then upgrades.

## PACTOR-4 (brief summary from Wavecom)

- 10 speed levels. Level 1: dual-channel chirp DBPSK at 66.667 baud.
- Levels 2–4: spread-spectrum DQPSK at 112.5 or 225 baud.
- Levels 5–10: single-carrier 1800 baud PSK/QAM at 1500 Hz center.
  Modulation: BPSK/QPSK/8PSK/16QAM/32QAM. Adaptive equalizer.
- Code rates: 1/3 and 5/6.
- Up to 9000 bps raw, 5512 bps user data (level 10).

---

## Feasibility assessment for ham-modem

### What we CAN build (PACTOR-I)
PACTOR-I is fully documented between the public protocol descriptions and the
hfterm GPL source code. We have:
- Complete frame format (header, data, status, CRC)
- CRC algorithm (reflected CRC-CCITT, init 0xFFFF, final invert)
- Control signals (CS1–CS4, 12-bit codes)
- Huffman table (128 entries)
- Status byte bit layout
- ARQ state machine (ISS/IRS, speed changes, break-in, QRT)
- Memory ARQ concept

This is enough to build a full PACTOR-I implementation that interoperates
with real PTC modems.

### What we CAN partially build (PACTOR-II)
We can build the physical layer (two-tone DPSK modulation/demodulation,
100 bd per tone, 200 Hz spacing) and detect/classify PACTOR-II signals.
The Viterbi decoder is standard. However, without:
- The generator polynomials (strong guess: 561/753, but unverified)
- The puncture patterns
- The interleaver
- The 40-bit CS codes
- The header format

...we cannot decode or encode frames that interoperate with real P-II gear.

### Recommended path

1. **Implement PACTOR-I first** — we have everything needed. This gives us
   legacy BBS and Winlink interop at 200 bps.

2. **Build a PACTOR-II waveform analyzer** — two-tone DPSK demod, symbol
   timing recovery, classification of DBPSK/DQPSK/8DPSK/16DPSK. This works
   from the public specs alone.

3. **Attempt to recover the missing parameters from recordings:**
   - Record PACTOR-II sessions from KiwiSDR/WebSDR receivers.
   - Use the known frame timing (1.25 s / 3.75 s cycles) to segment frames.
   - Try the assumed (561, 753) polynomials and standard puncture patterns.
   - The interleaver can potentially be brute-forced given enough data.
   - The 40-bit CS codes can be extracted by correlating the CS slots across
     many frames (they're always DBPSK, which we can demodulate).

4. **PACTOR-III and IV** — the OFDM carrier layout is known, but the coding
   and ARQ details are even less documented. Defer.

---

## Sources

- Helfert/Rink (DL6MAA/DL2FAK), "PACTOR-II Basics" — https://www.qsl.net/f5ru/pactorII.htm
- ARRL PACTOR-II page — http://www.arrl.org/pactor-ii
- ARRL PACTOR-I page — https://www.arrl.org/pactor
- NB6Z PACTOR details — https://myplace.frontier.com/~nb6z/pactor.htm
- SigIDWiki PACTOR II — https://sigidwiki.com/wiki/PACTOR_II
- Wavecom monitoring help — https://www.wavecom.ch/content/ext/MonitoringSystemOnlineHelp/worddocuments/pactorii.htm
- Wavecom "Advanced Protocol PACTOR" PDF — https://www.wavecom.ch/content/pdf/advanced_protocol_pactor.pdf
- hfterm PACTOR-I source (GPL) — https://github.com/yarda/hf (hfkernel/fsk/pactor.c)
- pdn-soundmodem issue #184 (signal measurements) — https://github.com/packet-net/pdn-soundmodem/issues/184
- ecjones.org PACTOR description — http://ecjones.org/pactor.html
