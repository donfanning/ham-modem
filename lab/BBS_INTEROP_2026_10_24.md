# LinBPQ and JNOS BBS application checks — 2026-10-24

## LinBPQ BPQMail

The first node-only image had `BBS=1` and an application call, but did not start the mail application. Adding the
`LINMAIL` directive made LinBPQ start BPQMail and create its default `linmail.cfg`. A local Telnet session to the
node's lab console (`172.31.73.3:8010`, configured local credentials) reached the BBS with `BBS` and displayed the BPQMail
prompt.

For the live check, a separate ham-modem TNC-2 station (`N0BBB`) connected through the SIP/PCMU ConfBridge to
`KL7EET-2`. The station received the BPQMail banner and name prompt, then logged in as `Don Fanning`. It submitted
message 2 with title `BBS INTEROP TEST`. BPQMail accepted and stored the message, then reported that it had no
local recipient or forwarding information, so the message was not delivered. This proves live AX.25 access to
the BBS application and message composition/storage, not delivery to a second BBS.

## JNOS mailbox and JNOS-to-LinBPQ session

The initial JNOS configuration attached the AX.25 interface but did not start its AX.25 server. Adding
`start ax25` to `autoexec.nos` fixed inbound mailbox access. A TNC-2 station connected to `KL7EET-4`, received the
JNOS 2.0q mailbox banner, registered as Don Fanning, and submitted a test message. JNOS acknowledged the
submission with `Msg queued`.

For the cross-BBS application check, JNOS initiated an AX.25 connection to LinBPQ's BBS application call
`KL7EET-2`. The two containers used separate ham-modem KISS-TCP stations (`18002` for JNOS, `18001` for LinBPQ)
in the same ConfBridge room. JNOS received the BPQMail banner and prompt. From that session, a message addressed
to `N0BBB @ KL7EET` was accepted by BPQMail as message 1. The `N0BBB` station then connected to LinBPQ, listed
the message with `LM`, and read its body with `R 1`. The capture shows the AX.25 connection, message submission,
acknowledgements, listing, and readback.

The two station logs are in
[`results/bbs_interop_2026_10_24/`](results/bbs_interop_2026_10_24/).

## Scope and next check

Both BBS applications now accept live AX.25 sessions, and a message sent through a JNOS-initiated session to
LinBPQ was stored and retrieved by a second station. This validates manual mailbox use and message storage across
the live channel. Bidirectional inter-BBS FBB forwarding now passes, including reverse delivery into a JNOS
user's private mailbox and readback by that user. JNOS scheduled forwarding also passed after adding
`mbox timer 30` to the lab startup configuration; one scheduled FBB session sent JNOS mail to LinBPQ
and received the pending reverse message. Durable mail-spool retention across container recreation
also passed using a named Docker volume; the private message remained readable after force-recreating
JNOS. No radio transmitter was used. The earlier connected-chat checks are in the
[container interop report](LINBPQ_JNOS_INTEROP_2026_10_24.md).

The follow-up FBB checks initially found a broken local DNS cache entry and a rewrite collision,
then exposed the missing JNOS mailbox-forwarding timer. After correcting these, both directions
transferred on a scheduled session and recipient stations listed and read the messages. Details and
logs are in the [FBB forwarding report](JNOS_FBB_FORWARD_2026_10_24.md).
