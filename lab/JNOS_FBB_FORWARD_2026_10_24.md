# JNOS to LinBPQ automatic FBB forwarding — 2026-10-24

## Configuration and checks

The lab runs JNOS 2.0q as `N0JNO-4` and LinBPQ BPQMail as `KL7EET-2`. JNOS uses
`mbox fbb 2`, starts its AX.25, forwarding, and SMTP services, and checks both the forwarding and
outgoing SMTP queues every 30 seconds (`mbox timer 30`, `smtp timer 30`, configured for this lab).
Its local DNS cache includes standard `localhost.` and `n0jno.` A records for
`127.0.0.1`, which lets the JNOS SMTP client route messages to the local SMTP server. The
forwarding area is `klbbs` (five characters); rewrite maps `*@kl7eet klbbs`; the forward record
associates `klbbs` with partner `KL7EET` over `ax25 vhf KL7EET-2`.

The initial blocker was the `domain.txt` record order. JNOS expects standard DNS record order
(`hostname IN A address`); address-first entries did not resolve the local hostname. After
correcting the records, the queued test message was delivered into `spool/mail/klbbs.txt`, as
shown by `mail.log` (`deliver: To: klbbs`).

## Result

Bidirectional FBB forwarding passed both by manual kick and by the scheduled forwarding timer. The
initial JNOS configuration had `smtp timer 30` but no `mbox timer`, so the forwarding service did not
initiate scheduled sessions. Adding `mbox timer 30` to `autoexec.nos` fixed this. After rebuilding
and starting a fresh JNOS container, a queued message caused the timer to initiate FBB without a
manual kick.

The earlier manual JNOS-to-LinBPQ check used `FBB DNS LOOPBACK TEST`; JNOS routed it into `klbbs`,
and a second station listed and read the transferred BPQMail message. One residual `.usr` area index
is expected and was present alongside the delivered `.txt` message.

For the scheduled run, BPQMail had `SCHEDULED AUTO POLL CHECK` queued to `N0BBB @ N0JNO`; LinBPQ's
forwarding configuration had reverse forwarding enabled. The timer initiated an FBB session, and
the same session transferred that inbound message. JNOS created `spool/mail/n0bbb.txt`, and
`mail.log` recorded delivery to `n0bbb`. The `N0BBB` peer listed and read it from its private
mailbox. The earlier manual reverse test also confirmed local mailbox delivery. The first reverse
attempt exposed a rewrite collision:
`*@n0jno klbbs` redirected messages addressed to local JNOS users into the forwarding area. Changing
the rewrite to `*@kl7eet klbbs` separated the remote forwarding address from the JNOS local hostname.
After that change, a fresh `N0BBB @ KL7EET` message submitted in JNOS appeared in `klbbs` and was
transferred to BPQMail. The peer listed and read BPQMail message 4, `OUTBOUND KL7EET DOMAIN TEST`.

Live station evidence is in [`results/jnos_fbb_2026_10_24/`](results/jnos_fbb_2026_10_24/) and
[`results/jnos_reverse_fbb_2026_10_24/`](results/jnos_reverse_fbb_2026_10_24/); the latter captures
the reverse delivery and outbound follow-up exchange. The scheduled timer run is in
[`results/jnos_scheduled_fbb_2026_10_24/`](results/jnos_scheduled_fbb_2026_10_24/).
The forwarding pattern follows the [JNOS forwarding notes](https://kb8ojh.net/packet/jnos.html#mail-and-bulletins).

## Remaining checks

Scheduled forwarding without a manual kick passed after a fresh JNOS container start. A named
`jnos-spool` volume now retains JNOS's spool across container recreation. In the persistence check,
JNOS received a private message, the container was force-recreated, the mailbox still showed one new
message, and the peer read the original body. The message file's SHA-256 was unchanged across
recreation. The 30-second timer is intentionally short for the lab; production scheduling should use
an interval and permitted contact hours appropriate to the station and channel. LinBPQ's
`DontHoldNewUsers=1` was used so the temporary lab peer's new-user message could be forwarded. No
radio transmitter was used; this was a confined SIP/PCMU ConfBridge lab.
