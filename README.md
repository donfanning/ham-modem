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
| CSMA channel, KISS pty/TCP, TNC-2, AGWPE | next (phases 2 and 4) |
| RTP/SIP/IAX2 + ConfBridge lab | phase 3 |
| G3RUH 9600 over L16/16 kHz | phase 6 |

## Try it

```sh
python3 -m venv .venv && .venv/bin/pip install numpy
.venv/bin/python -m unittest discover -s tests -t .
.venv/bin/python tools/afsk_wav.py gen /tmp/test.wav --count 20
.venv/bin/python tools/afsk_wav.py decode /tmp/test.wav -v
```

License: GPL-3.0-or-later (see [NOTICE](NOTICE)).
