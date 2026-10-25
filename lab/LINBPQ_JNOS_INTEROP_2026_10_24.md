# LinBPQ and JNOS container interoperability — 2026-10-24

## Setup

Built and started the two services with:

```sh
docker compose -f lab/interop/compose.yaml up --build -d
```

LinBPQ **6.0.26.1** was built from commit `228047d76f410e13a564fb00787516f69b96ff44`. JNOS **2.0q** was built
from the maintainer's `rsync://www.langelaar.net/official/2.0q/` release module. Both joined the isolated
`ham-modem-lab` bridge network. LinBPQ connected to ham-modem's KISS-TCP listener at `172.31.73.1:18001`; JNOS
used a PTY and `socat` to connect to the same listener.

The audio station was ham-modem AFSK1200 over SIP/PCMU to ConfBridge room 7300. A separate ham-modem TNC-2 station
provided the packet peer over that same room.

## LinBPQ KISS-TCP and connected chat

LinBPQ reported successful configuration parsing and initialized its `TCPKISS` port. Its Telnet node accepted
the configured lab login. From that node, `C 1 N0AAA` established an AX.25 connection to a TNC-2 PTY peer. The
peer received `LINBPQ_LIVE_CHAT`; ham-modem's monitor log recorded the SABM/UA exchange, the data I-frame, and an
RR acknowledgement:

```text
tx KL7EET>N0AAA <SABM P>
rx N0AAA>KL7EET <UA F>
tx KL7EET>N0AAA <I R0 S0 P>:LINBPQ_LIVE_CHAT
rx N0AAA>KL7EET <RR R1 F>
```

## JNOS KISS PTY and connected chat

The JNOS console showed the `vhf` AX.25 interface attached to `/dev/ttyTNC`, with local call `KL7EET-3` and BBS
call `KL7EET-4`. From the JNOS console, the session manager command `c vhf N0CCC` established an AX.25 session
with a TNC-2 PTY peer. JNOS sent `JNOS_LIVE_CHAT`, and the peer replied with `PEER_TO_JNOS_REPLY`. The JNOS
console displayed the reply. The ham-modem monitor captured:

```text
tx KL7EET-4>N0CCC <SABM P>
rx N0CCC>KL7EET-4 <UA F>
tx KL7EET-4>N0CCC <I R0 S0>:JNOS_LIVE_CHAT
rx N0CCC>KL7EET-4 <RR R1>
rx N0CCC>KL7EET-4 <I R1 S0>:PEER_TO_JNOS_REPLY
tx KL7EET-4>N0CCC <RR R1>
```

The full ham-modem monitor capture is in
[`results/tnc2_interop_2026_10_24/tnc.log`](results/tnc2_interop_2026_10_24/tnc.log). Startup logs for the
station and peer are alongside it.

These runs validate KISS-TCP/PTY attachment and bidirectional AX.25 connected chat through the live VoIP channel.
They do not yet validate LinBPQ or JNOS BBS application sessions, FBB forwarding, NET/ROM, or operation with a
radio transmitter.
