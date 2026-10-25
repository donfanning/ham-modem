# TNC-2 multi-stream command port — 2026-10-24

`Tnc2Terminal` now manages up to ten simultaneous AX.25 links on one terminal port. It starts with four streams;
`MAXSTREAM n` changes the limit, `STREAM n` selects a zero-based stream from command mode, and Ctrl-\\ (ASCII
`0x1C`) cycles between active streams while in converse mode. `STREAM` with no argument lists each stream's peer
and state. The `MAXSTREAM` setting is included in the existing JSON NVRAM saved by `WRITE`.

The links share one callsign and link-parameter set, while each stream has its own peer, connection state, AX.25
sequence numbers, and transmit queue. Incoming connections occupy the first free stream when `CONOK` is enabled.
`MAXSTREAM` cannot be reduced below an occupied stream index; `KISS ON`, `RESTART`, and terminal reset disconnect
the streams.

The regression creates three simulated TNCs, connects one terminal to two peers concurrently on streams 0 and 1,
checks that each peer receives only its selected stream's chat data, and switches with both `STREAM` and Ctrl-\\.
The NVRAM round-trip also checks persistence of `MAXSTREAM`.

```sh
.venv/bin/python -m unittest tests.test_tnc2
```

The 2026-10-24 run passed **15 tests**. These are deterministic in-process audio-channel tests, not an interoperability
run against a third-party terminal or BBS application.
