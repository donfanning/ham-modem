# Milestone 1: 1200 bd AFSK through an Asterisk ConfBridge — 2026-10-21

Setup: `lab/asterisk` (Asterisk 22.10.1, host network, SIP 127.0.0.1:15060, RTP 20000-20100), room 7300
(`ham_bridge`: 8 kHz mixing, 20 ms interval; `ham_user`: no silence drop, no denoise, no jitter buffer, quiet).
Stations: `python -m hammodem.main --sip-dial 7300@127.0.0.1:15060`, PCMU, KISS-TCP; harness
`lab/confbridge_test.py` (frames from every station at random times, delivery matrix over KISS).

## Results

| Test | Result |
|---|---|
| Echo extension 7399, one station decodes itself | 3/3 |
| 3 stations, 0% loss (5 runs, 8-10 frames each) | 48/48, 46/48, 48/48, 48/48, 58/60, 60/60 |
| Direwolf 1.6 on station 0's received audio (`--audio-tap` -> `ADEVICE udp:`) | 16/16 of what station 0 heard |
| 3 stations, 1% simulated RTP receive loss | 51/60 (85%) |
| 3% loss | 35/60 (58%), 39/60 (65%) |
| 5% loss | 25/60 (42%) |
| 3% loss, TXDELAY 100 ms instead of 300 | 37/60 (62%) -- no gain |

RTP stats at 0% loss: no late/duplicate packets, no concealment, no bad FCS. The bridge does not echo a
station's own audio (no station ever decoded its own frames).

## Findings

1. **Residual 0%-loss misses are collisions, not DSP faults.** Each miss pattern is one frame from each of two
   senders lost at the *third* station only, with zero bad-FCS counts. Two stations keyed up inside the
   bridge's latency window (mixing interval + RTP), each heard the other cleanly (the bridge excludes our own
   audio), the third heard the sum.
   **Opportunity unique to VoIP:** because a station never hears itself in the bridge, *any* in-band energy
   while transmitting is a collision -- the TNC can do CSMA/CD (abort, back off, retry) like Ethernet, which
   RF cannot. Must be off on echo/P2P legs where we do hear ourselves. -> TODO.
2. **Loss hurts exactly as predicted**: delivery tracks the chance that a frame's *body* survives untouched.
   Losing preamble flags is harmless (repeat-last concealment just repeats flags), which is why shortening
   TXDELAY bought nothing. The levers are FEC (FX.25 / IL2P, plan phase 7 -- consider moving it earlier),
   shorter PACLEN, and connected-mode retries (phase 4).
3. Energy DCD makes CSMA work across the bridge (deferrals counted in every run; 8-28 per station).

## Addendum: lab isolation incident

The first version of `lab/asterisk/compose.yaml` had no project name. Compose derived `asterisk` from the
directory name, which is the same as El-Modem's `lab/asterisk`, and our service was also called `asterisk`.
`docker compose up -d` therefore recreated El-Modem's `asterisk-asterisk-1` as our PBX, which broke El-Modem's
tests and PBX work. The lab now has an explicit project (`ham-modem-lab`), its own network
(172.31.73.0/24, PBX at 172.31.73.10:5060), no host networking and no host ports. The results above were
measured on the old host-network setup on 127.0.0.1:15060; they were re-checked on the isolated lab below.

## Follow-ups (same day)

**300 bd AFSK through the room** (`--modem afsk300`, 3 stations x 4 frames): 24/24.

**CSMA/CD** (`--collision-detect`, finding 1 above). Implemented in `channel.py`: carrier heard while we transmit
-> cut the burst, requeue its frames at the head of the queue, back off 1..2^k slots, drop after 8 attempts.

| Test | plain CSMA | CSMA/CD |
|---|---|---|
| Simulation, 5 stations x 3 frames all queued at t=0, 8 seeds (zero-latency mixer) | 108/480 lost (22.5%) | 0/480 lost (11 aborts) |
| Live room 7300, 5 stations x 6 frames, ~1.5 s spacing, run 1 | 99/120 (82.5%) | 120/120 (2 aborts) |
| run 2 | 120/120 | 120/120 (0 aborts) |

The direction is consistent, but two live pairs are not conclusive; the gain shows up when stations contend.
It stays opt-in: on an echo leg we hear ourselves, and on a two-party call overlap is harmless (full duplex).
