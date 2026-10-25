# TNC-2 PTY and KISS-TCP interface checks — 2026-10-24

## TNC-2 command port over PTY

Three ham-modem stations joined the live lab ConfBridge (`7300@172.31.73.10`) over SIP/PCMU. Each station exposed
TNC-2 command mode on `--tnc2-pty`; a host-side byte-stream client opened the generated POSIX PTYs (the Linux
serial/COM-style path).

The `KL7EET` terminal connected to `N0AAA` on stream 0, returned to command mode with Ctrl-C, selected stream 1,
and connected to `N0BBB`. It entered converse mode on each stream and sent `COMPORT_STREAM_ZERO` and
`COMPORT_STREAM_ONE`. Each payload arrived at its intended peer. The peer terminals showed the matching text.
The CLI processes logged successful SIP/PCMU connections; station logs are in
[`results/tnc2_interfaces_2026_10_24/pty/`](results/tnc2_interfaces_2026_10_24/pty/).

A separate PTY command check queried the four-stream default, set `MAXSTREAM 10`, listed streams 0–9, selected
stream 9, and confirmed the expected `WRITE` response when no NVRAM file was configured.

This exercises the actual PTY endpoint and live AX.25 audio links. It does not test a physical UART, Windows
COM port, or a third-party terminal/BBS application.

## KISS-TCP with third-party packet software

Ran `LOG=lab/results/tnc2_interfaces_2026_10_24/tcp bash lab/realapps_test.sh`. The existing isolated lab script
connected Direwolf 1.7 to the audio hub as an on-air peer and Direwolf's `kissutil` to ham-modem's KISS-TCP
server. `kissutil` sent `N0CALL>APRS,WIDE1-1:>hello via the Direwolf digipeater`; Direwolf retransmitted it via
`DW1TST`, and `kissutil` received `N0CALL>APRS,DW1TST*:>hello via the Direwolf digipeater`. Direwolf also
beaconed `DW1TST>APDW17:>Direwolf peer on the VoIP channel`, which ham-modem received.

Captured output and logs are in [`results/tnc2_interfaces_2026_10_24/tcp/`](results/tnc2_interfaces_2026_10_24/tcp/).
This validates KISS-TCP packet exchange; TNC-2 command mode itself is not exposed over TCP.
