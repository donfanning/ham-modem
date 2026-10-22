# Phase 4: AX.25 connected mode, TNC-2 command mode, AGWPE — 2026-10-21

## Unit and simulation tests (63 total, all passing)

- `tests/test_ax25_link.py`: link state machine against a simulated channel with a fake clock:
  connect/exchange/disconnect, 2000-byte transfer (63 I frames, sequence wraparound), **20% frame loss on
  5 seeds, all data delivered intact via REJ/poll recovery**, DM busy, retry-count exceeded (SABM + N2
  retries exactly), link failure when the peer vanishes, digipeater path with reply-path reversal.
- `tests/test_tnc2.py`: command parsing (abbreviations, ?EH, ranges, backspace/echo, KISS ON and FEND FF FEND
  return) and two stations over *modulated audio* in simulated time: connect, chat, disconnect, UNPROTO
  via path, CONOK OFF -> busy.
- `tests/test_agw.py`: real AGW TCP client: R/G/X, UI out and monitor in, connect/data/disconnect,
  incoming connect only once the callsign is registered (unowned calls are silent on the air, so the
  caller's SABM retry succeeds after registration).

## Live, through the isolated lab PBX (room 7300 @ 172.31.73.10)

Station A: `--tnc2-pty --mycall N0CALL`; station B: `--agw 18200`, client registered K1ABC.
`C K1ABC` -> `*** CONNECTED to K1ABC`, text both ways, `DISC` -> `*** DISCONNECTED`; AGW side saw
`CONNECTED With Station N0CALL`, the data, and `DISCONNECTED From Station N0CALL`. Both stations: bad FCS 0,
no RTP loss or concealment, CSMA deferrals 4 and 6.

## Not yet

Modulo-128/SABME, SREJ, multiple TNC-2 streams, settings persistence, real-application checks
(Outpost/UI-View/Xastir/LinBPQ/kissattach are not installed on this host).
