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
| TNC-2 command mode, AX.25 connected mode, AGWPE | phase 4 |
| RTP/SIP/IAX2 + ConfBridge lab | phase 3 |
| G3RUH 9600 over L16/16 kHz | phase 6 |

## Try it

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

```sh
python3 -m venv .venv && .venv/bin/pip install numpy
.venv/bin/python -m unittest discover -s tests -t .
.venv/bin/python tools/afsk_wav.py gen /tmp/test.wav --count 20
.venv/bin/python tools/afsk_wav.py decode /tmp/test.wav -v
```

License: GPL-3.0-or-later (see [NOTICE](NOTICE)).
