# Ham-Modem

A software packet-radio TNC whose "radio" is a VoIP call. Stations dial an Asterisk ConfBridge room (the shared
channel, like a simplex frequency) or call each other directly over SIP/IAX2, and exchange AX.25 packets as
audio — Bell 202 1200 bd AFSK, 300 bd HF AFSK, and later 9600 bd G3RUH. Legacy packet software talks to it as if
it were a hardware TNC: KISS over a serial port or pty, TNC-2 command mode, KISS-over-TCP and AGWPE.

Sibling of [El-Modem](https://github.com/donfanning/el-modem) (Hayes dial-up modem over VoIP).
Design, research notes and phases: [docs/PLAN.md](docs/PLAN.md).

## Status

| Piece | State |
|---|---|
| HDLC / NRZI / FCS, AX.25 frame codec, KISS codec | done, unit-tested |
| AFSK 1200 / 300 modulator + demodulator (slicer bank, PLL, DCD) | done; interoperates with Direwolf both ways ([lab note](lab/AFSK_BASELINE_2026_10_21.md)) |
| CSMA channel (p-persist, DCD from timing quality + in-band energy), KISS pty / tty / TCP | done; live test through `tools/audio_hub.py` with Direwolf monitoring |
| AX.25 v2.0 connected mode (mod 8, REJ/poll recovery, digipeater paths), TNC-2 command mode (`--tnc2-pty`), AGWPE server (`--agw`) | done; live TNC-2 <-> AGWPE session through the ConfBridge ([lab note](lab/CONNECTED_MODE_2026_10_21.md)) |
| SIP (dial / listen, digest), RTP (PCMU/PCMA/L16, loss concealment), Asterisk ConfBridge lab | **milestone 1 reached**: 3 stations in a ConfBridge, Direwolf monitoring ([lab note](lab/CONFBRIDGE_MILESTONE1_2026_10_21.md)) |
| IAX2 legs (`--iax-dial EXT@PBX`, `--iax-listen PBX`; G.711, AFSK modes) | done; mixed IAX2 + SIP room 48/48 ([lab note](lab/IAX2_2026_10_21.md)) |
| G3RUH 9600 bd (`--modem g3ruh9600`, L16/16 kHz legs, room 7316) | done; Direwolf interop both ways, 48/48 through the 16 kHz ConfBridge ([lab note](lab/G3RUH_9600_2026_10_21.md)) |
| G3RUH 19200 / 38400 bd (`--modem g3ruh19200` / `g3ruh38400`, L16/48 kHz legs, room 7348) | done; 19200 interoperates with Direwolf; live 48/48 in the 48 kHz room with CSMA/CD ([lab note](lab/G3RUH_FAST_2026_10_21.md)) |
| PAM-4 57.6 kbit/s VoIP-native mode (`--modem pam57600`, 48 kHz legs; `pam19200` on 16 kHz) | done; live 48/48 in room 7348, 96.7% at 3% RTP loss vs 58% for AFSK 1200 ([lab note](lab/PAM4_57600_2026_10_21.md)) |
| FX.25 FEC (`--fx25 16/32/64`; receive always on), Direwolf-compatible | done; interop both ways under damage; 1200 bd at 3% loss: 50% -> 96.7% ([lab note](lab/FX25_2026_10_21.md)) |
| Loss resilience: erasure-aware FX.25 decoding (RTP concealment -> RS erasures) and `--repeat N` time diversity | done; 9600 FX.25/32 at 3% loss 55 -> 59/60, 57.6k at 5% loss 85% -> 100% ([lab note](lab/LOSS_RESILIENCE_2026_10_21.md)) |
| IL2P (`--il2p on|max`; receive always on for AFSK/G3RUH) | done; interoperates with Direwolf 1.7 both ways incl. RS repair; for loss, prefer FX.25 ([lab note](lab/IL2P_2026_10_21.md)) |
| Passband QAM 38.4k / 51.2k on 16 kHz legs (`--modem qam38400` / `qam51200`, room 7316) | done; live 48/48 in the 16 kHz room ([lab note](lab/QAM_2026_10_21.md)) |
| CSMA/CD for rooms (`--collision-detect`) | done, opt-in |

## Try it

Through a real Asterisk conference room (the lab PBX lives on its own Docker network, 172.31.73.10, and binds nothing on the host):

```sh
(cd lab/asterisk && docker compose up -d)
export PYTHONPATH=$PWD
.venv/bin/python -m hammodem.main --sip-dial 7300@172.31.73.10 --kiss-tcp 8001 -m &
.venv/bin/python -m hammodem.main --sip-dial 7300@172.31.73.10 --kiss-pty ~/.ham-modem1 -m &
.venv/bin/python lab/confbridge_test.py --stations 3 --frames 10 [--loss 0.03]   # delivery matrix
```

A dumb-terminal user: `--tnc2-pty ~/.ham-tnc2 --mycall N0CALL`, then `minicom -D ~/.ham-tnc2` / `screen ~/.ham-tnc2`
and type `C K1ABC` at the `cmd:` prompt. AGWPE clients: `--agw 8000`.

9600 bd: `--modem g3ruh9600 --sip-dial 7316@172.31.73.10` (16 kHz room; 8 kHz G.711 legs are refused).
19200/38400 bd: `--modem g3ruh19200 --sip-dial 7348@172.31.73.10` (48 kHz room, L16/48000).
57.6 kbit/s: `--modem pam57600 --sip-dial 7348@172.31.73.10` (VoIP-only waveform; same KISS/TNC-2/AGW interfaces).
51.2 kbit/s on ordinary 16 kHz legs: `--modem qam51200 --sip-dial 7316@172.31.73.10 --txdelay 20`.

Point-to-point (no PBX): one station runs `--sip-listen --sip-port 5070`, the other `--sip-dial x@HOST:5070`.


Two TNCs on a local stand-in for a conference bridge (raw PCM over UDP), Direwolf-compatible:

```sh
export PYTHONPATH=$PWD
.venv/bin/python tools/audio_hub.py --port 7300 &                       # the "frequency"
.venv/bin/python -m hammodem.main --udp-audio 7401:127.0.0.1:7300 --kiss-tcp 8001 -m &
.venv/bin/python -m hammodem.main --udp-audio 7402:127.0.0.1:7300 --kiss-pty ~/.ham-modem1 -m &
.venv/bin/python tools/kiss_tool.py --dev ~/.ham-modem1 listen &
.venv/bin/python tools/kiss_tool.py --tcp 127.0.0.1:8001 send N0CALL APRS ">hello"
```

`kissattach ~/.ham-modem1 radio` (Linux AX.25 tools) works on the pty the same way. Offline:

IAX2 needs the native binding (Rust + maturin; unset ARCHFLAGS if your shell sets it):

```sh
env -u ARCHFLAGS maturin build --release -m native/iax/Cargo.toml -i .venv/bin/python3.12
.venv/bin/pip install native/iax/target/wheels/ham_modem_iax-*.whl
HAM_MODEM_IAX_SECRET=... .venv/bin/python -m hammodem.main --iax-dial 7300@172.31.73.10 --kiss-tcp 8001
```

```sh
python3 -m venv .venv && .venv/bin/pip install numpy
.venv/bin/python -m unittest discover -s tests -t .
.venv/bin/python tools/afsk_wav.py gen /tmp/test.wav --count 20
.venv/bin/python tools/afsk_wav.py decode /tmp/test.wav -v
```

License: GPL-3.0-or-later (see [NOTICE](NOTICE)).
