# Ham-Modem

**Packet radio and digital modes, with VoIP audio as the channel.**

I’m Don Fanning (KL7EET), creator of [El-Modem](https://github.com/donfanning/el-modem), an experimental Hayes and
V-series software modem over VoIP. Ham-Modem is its packet-radio companion: it provides a software TNC and digital
modem modes that can share an Asterisk conference room or connect directly over SIP. The projects share some
transport and test-lab foundations; see [NOTICE](NOTICE) for code provenance and attribution.

Ham-Modem is an active research project. Some features have offline regression tests, some have live VoIP or
Direwolf interoperability results, and experimental HF modes have not been validated against commercial radio
equipment. The [validation guide](docs/TESTING_VALIDATION.md) spells out what each result establishes.

## What’s inside

- **Packet TNC:** AX.25 connected and UI frames, KISS over pty/serial/TCP, AGWPE, and TNC-2 command mode. TNC-2
  supports multiple simultaneous links, configurable `MAXSTREAM`, and JSON settings persistence with `--nvram`.
- **VoIP radio channel:** SIP/RTP with PCMU, PCMA, and L16 audio; IAX2 with G.711; Asterisk ConfBridge for a shared
  channel; and direct SIP calls for point-to-point links.
- **System audio:** full-duplex capture/playback through PortAudio or PulseAudio/PipeWire devices, including
  virtual audio cables for connecting modem audio to a radio's normal mic/speaker jacks. See [system audio setup](docs/AUDIO_DEVICES.md).
- **Packet waveforms:** AFSK 300/1200 baud, G3RUH 9600/19200/38400 baud, Direwolf-compatible 2400 QPSK and 4800
  8PSK, passband QAM at 38.4/51.2 kbit/s, and VoIP-native PAM-4 at 19.2/57.6 kbit/s.
- **Link resilience:** FX.25 and IL2P FEC, AX.25 retry/recovery, `--repeat` time diversity, CSMA/CD for shared rooms,
  and receive-side erasure handling.
- **Experimental digital modes:** PACTOR-II/III, Clover-II, ARDOP, FreeDATA/Codec2, keyboard modes, and FAX/SSTV
  image tools. Their implementation and test depth vary.

## Validation highlights

- **660 unit tests passed** on 2026-10-24. Run the suite locally with the command below.
- **Direwolf interoperability** is documented for AFSK, G3RUH, PSK, FX.25, and IL2P in their dated lab reports.
- **Live packet-loss trial:** under a filtered 3% station-to-PBX RTP drop, FX.25/32 delivered 10/10 frames in both
  repeats; without FEC, delivery was 5/10 and 7/10. The test used one Asterisk/PCMU lab path, so it is a measured
  result for that setup rather than a general link rating.
- **Experimental HF modes:** local peer tests do not establish interoperability with SCS or HAL equipment.

Browse [dated lab reports](lab/) for configurations, summarized outcomes, and limits. Transient station logs are
excluded from the public repository. The [project plan](docs/PLAN.md) tracks remaining work.

## Install and run

Python 3.11 or newer is required. From the repository root:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -t .
```

### Join the local Asterisk lab

Start the isolated PBX, then launch a station with KISS-TCP:

```sh
(cd lab/asterisk && docker compose up -d)
.venv/bin/python -m hammodem.main --sip-dial 7300@172.31.73.10 --kiss-tcp 8001 --modem afsk1200
```

Run a multi-station delivery trial with:

```sh
.venv/bin/python lab/confbridge_test.py --stations 3 --frames 10
```

The repeatable impairment and results workflows are recorded in [TESTING_VALIDATION.md](docs/TESTING_VALIDATION.md).

### Use TNC-2 command mode

Create a terminal pty and connect it to a peer:

```sh
.venv/bin/python -m hammodem.main --sip-dial 7300@172.31.73.10 \
  --tnc2-pty ~/.ham-tnc2 --mycall N0CALL --nvram ~/.ham-tnc2.json --modem afsk1200
```

Open `~/.ham-tnc2` with `minicom` or `screen`, then enter `C K1ABC` at the `cmd:` prompt. TNC-2 starts with four
streams. Use `STREAM 0` through `STREAM 3` to select one; in converse mode, press Ctrl + backslash (ASCII `0x1C`)
to cycle between active links. `MAXSTREAM 1` through `MAXSTREAM 10` sets the stream limit. `WRITE` saves settings
to the configured JSON NVRAM file. See the [multi-stream lab note](lab/TNC2_MULTISTREAM_2026_10_24.md).

## Selected modem examples

| Mode | Example |
|---|---|
| G3RUH 9600 baud | `--modem g3ruh9600 --sip-dial 7316@172.31.73.10` (16 kHz room) |
| G3RUH 19200 baud | `--modem g3ruh19200 --sip-dial 7348@172.31.73.10` (48 kHz room) |
| G3RUH 38400 baud | `--modem g3ruh38400 --sip-dial 7348@172.31.73.10` (48 kHz room) |
| PAM-4 57.6 kbit/s | `--modem pam57600 --sip-dial 7348@172.31.73.10` |
| Passband QAM 51.2 kbit/s | `--modem qam51200 --sip-dial 7316@172.31.73.10 --txdelay 20` |
| Direct SIP, no PBX | Listener: `--sip-listen --sip-port 5070`; caller: `--sip-dial x@HOST:5070` |

G.711 is refused for the 16 kHz G3RUH path; use the matching L16-capable room. The [lab reports](lab/) describe
room rates and other mode-specific requirements.

## Project notes

- [Plan and open work](docs/PLAN.md)
- [Tests, live trials, and validation limits](docs/TESTING_VALIDATION.md)
- [Code and research attribution](NOTICE)

Licensed under [GPL-3.0-or-later](LICENSE).
