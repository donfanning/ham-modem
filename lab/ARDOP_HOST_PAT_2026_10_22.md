# Phase 10.4: ardopcf-compatible host interface; Winlink (Pat) P2P through ham-modem — 2026-10-22

`ham-modem --modem ardop` runs an ARDOP TNC on any of our audio legs (SIP, IAX2, UDP; resampled to ARDOP's 12 kHz
internally) with the host interface Winlink clients use with ardopcf:

    ham-modem --modem ardop --mycall K1ABC --ardop-host 8515 --arqbw 500MAX --sip-dial 7300@pbx.example

* `hammodem/ardop/host.py`: TCP command port N (CR-terminated lines; `CMD value` / `CMD now value` /
  `FAULT ...`; asynchronous NEWSTATE, CONNECTED, DISCONNECTED, BUFFER, PTT, STATUS, PENDING, TARGET, PINGREPLY,
  ...) and data port N+1 (`len_hi len_lo data` in; `len_hi len_lo TAG data` out, TAG `ARQ` / `IDF`). Commands:
  INITIALIZE, MYCALL, GRIDSQUARE, ARQBW, CALLBW, ARQTIMEOUT, ARQCALL, DISCONNECT, ABORT/DD, BREAK, LISTEN,
  AUTOBREAK, FASTSTART, USE600MODES, LEADER, TRAILER, DRIVELEVEL, PROTOCOLMODE (ARQ only), PING, SENDID, STATE,
  BUFFER, PURGEBUFFER, VERSION, plus ardopcf's other settings accepted for compatibility; FEC commands FAULT.
* `hammodem/ardop/tnc.py`: buffers received audio and clocks the station on the leg's 20 ms send tick.

## Results

| test | result |
|---|---|
| tests/test_ardop_host.py: two TNCs back to back, each behind its own host interface, driven by socket clients | connect, data both ways, disconnect, ID frame on the data port |
| lab/ardop/live_hosts.py: `ham-modem --modem ardop` vs ardopcf, both driven only through host interfaces | 2000 B + 1000 B intact, 500 Hz, 95 s, shifted up to 16QAM.500 |
| lab/ardop/pat_p2p_test.sh: Pat 1.0.0 (K1ABC) on ham-modem <-> ardopcf with Pat (N0CALL) listening | B2F P2P session delivers both messages: 3/3 runs at 500 Hz, 1/1 at 2000 Hz, ~86 s each, no FAULTs, clean disconnect in 3 s |

Pat's command set against us: INITIALIZE, MYCALL, GRIDSQUARE, ARQBW, ARQTIMEOUT, CWID, LISTEN, PROTOCOLMODE,
VERSION, STATE, BUFFER, ARQCALL, DISCONNECT, ABORT -- all handled.

## Fixes found by the live runs

* An IRS only sends DISC in reply to IDLE (as ardopcf), so a host DISCONNECT could wait for the session timeout if
  the ISS went quiet. Now DISC also replaces the ACK/NAK to a data frame (spec rule 1.6), and a pending
  DISCONNECT with nothing heard for 6 s sends DISC anyway; END ends the session in any state while DISC repeats.
* Pat (wl2k-go `conn.Close`) can send a second DISCONNECT after the session ended and then wait 30 s for
  DISCONNECTED / NEWSTATE DISC before ABORTing (seen in 1 of 3 runs; ardopcf answers `DISCONNECT IGNORED` too, so
  it would hit the same race). We now repeat `NEWSTATE DISC` after `DISCONNECT IGNORED` within 10 s of a session
  ending -- a deliberate deviation from ardopcf.

## Lab hygiene: Pat and winlink.org

Pat looks up MYCALL at winlink.org (`cmsapi.AccountExists`) before connecting, and with no account offers an
"over-the-air activation". The first attempt here did that lookup once for K1ABC (no connection to a CMS was made
and nothing was activated; the prompt was never answered). The test now maps winlink.org / getpat.io to 127.0.0.1
in the Pat containers; with the API unreachable Pat falls back to "account exists if the mailbox is not empty",
so the script seeds each mailbox's sent folder. Nothing leaves the lab network.

## Next

* More VoIP-leg conditions (RTP loss, IAX2, third station in the room).
* FEC mode (PROTOCOLMODE FEC, FECSEND), summation memory ARQ, BUSY detection, 10-minute ID in long sessions.

## ARDOP across the ConfBridge room (lab/ardop/confbridge_ardop_test.sh)

`ham-modem --modem ardop --sip-dial 7300@172.31.73.10` (8 kHz PCMU/PCMA, resampled to ARDOP's 12 kHz by `Phy`) and
ardopcf (UDP audio -> `lab/ardop/sip_bridge.py` -> resample 12k<->8k -> SIP leg) join the same Asterisk ConfBridge
room 7300, each driven only through its host interface by `live_hosts.py --external-tnc`:

| session | result |
|---|---|
| ours calls, 1500 B + 800 B, 500 Hz | both intact, 79 s, shifted up to 16QAM.500 |
| ardopcf calls, 6000 B + 3000 B, 2000 Hz | both intact, 113 s, 13 data frames all ACKed first time, 0 repeats |

So ARDOP survives G.711 companding, the PBX's room mixer and two resamplers with no RS corrections needed.
