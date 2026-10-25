# LinBPQ and JNOS interop containers

These images run on the existing isolated `ham-modem-lab` Docker network. They publish no host ports. LinBPQ uses
KISS-TCP directly; JNOS uses `socat` to bridge KISS-TCP to a POSIX PTY, then attaches that PTY as a serial KISS
TNC.

## Versions and builds

- LinBPQ **6.0.26.1**, upstream commit `228047d76f410e13a564fb00787516f69b96ff44`.
- JNOS **2.0q**, source from the maintainer's versioned `official/2.0q` rsync module.
- Both use the pinned Debian Trixie base image digest in their Dockerfiles.

Build and start both:

```sh
docker compose -f lab/interop/compose.yaml up --build -d
docker compose -f lab/interop/compose.yaml ps
docker compose -f lab/interop/compose.yaml logs --tail=80
```

Stop them after a lab run:

```sh
docker compose -f lab/interop/compose.yaml down
```

JNOS stores its spool in the Compose named volume `interop_jnos-spool`; a normal `down` preserves that data.
Use `docker compose -f lab/interop/compose.yaml down -v` only when you intend to erase the lab mail spool.

The configuration is for an isolated software lab. LinBPQ uses `KL7EET-1` / `KL7EET-2` and KISS-TCP port
`18001`; JNOS uses `KL7EET-3` / `KL7EET-4` and KISS-TCP port `18002`. Run one ham-modem station for each node;
both join ConfBridge room 7300. Do not attach a real transmitter to this configuration.

Before starting the containers, replace the `SET_A_LOCAL_LAB_PASSWORD`, `SET_LOCAL_USERNAME`, and
`SET_LOCAL_PASSWORD` placeholders in `linbpq/bpq32.cfg` and `SET_A_LOCAL_LAB_PASSWORD` in
`jnos/autoexec.nos` with credentials chosen for your isolated lab. No working passwords are included in the
repository.

## Live channel test

Start a ham-modem audio/KISS-TCP station for LinBPQ on the lab host:

```sh
.venv/bin/python -m hammodem.main \
  --sip-dial 7300@172.31.73.10 --local-ip 172.31.73.1 \
  --kiss-tcp 0.0.0.0:18001 --mycall KL7EET-1 --txdelay 100 --monitor
```

In another terminal, start a separate station for JNOS:

```sh
.venv/bin/python -m hammodem.main \
  --sip-dial 7300@172.31.73.10 --local-ip 172.31.73.1 \
  --kiss-tcp 0.0.0.0:18002 --mycall KL7EET-3 --txdelay 100 --monitor
```

Start a TNC-2 peer in a second terminal, using a PTY opened by a terminal program or a small byte-stream client:

```sh
.venv/bin/python -m hammodem.main \
  --sip-dial 7300@172.31.73.10 --local-ip 172.31.73.1 \
  --tnc2-pty /tmp/n0aaa --mycall N0AAA --modem afsk1200
```

For LinBPQ, connect to the host-side bridge address shown by `docker inspect ham-lab-linbpq`, TCP port 8010.
Log in with the username and password you configured; issue `C 1 N0AAA`, then type a short message. The N0AAA
PTY should show the connected status and message.

For JNOS, attach to its console with `docker attach --sig-proxy=false ham-lab-jnos`. At `jnos>`, enter `session`,
then `c vhf N0AAA`; after the AX.25 session connects, type a short message. The N0AAA PTY should receive it.
Use Docker's `Ctrl-P`, `Ctrl-Q` detach sequence to leave the console running.

The 2026-10-24 link/chat run and evidence are recorded in
[`../LINBPQ_JNOS_INTEROP_2026_10_24.md`](../LINBPQ_JNOS_INTEROP_2026_10_24.md).
The follow-up BBS checks, including the JNOS mailbox issue, are recorded in
[`../BBS_INTEROP_2026_10_24.md`](../BBS_INTEROP_2026_10_24.md).
