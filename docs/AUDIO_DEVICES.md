# System audio devices

Ham-Modem can use a computer's audio capture and playback devices as its radio audio leg. This supports standard
mic/speaker connections and virtual audio cables without changing the modem interface. In auto mode the CLI uses
PulseAudio or PipeWire's Pulse compatibility server when its `pactl`, `parec`, and `pacat` tools are available;
otherwise it uses PortAudio through the optional `sounddevice` package.

Install and inspect devices:

```sh
python -m pip install -e '.[audio]'   # PortAudio backend
ham-modem --list-audio-devices
```

PulseAudio/PipeWire devices are listed by server endpoint name. PortAudio devices can be selected by index or a
unique name substring. Select capture and playback together:

```sh
ham-modem --modem afsk1200 --audio-in 'USB Audio: Audio (hw:1,0)' \
  --audio-out 'USB Audio: Audio (hw:1,0)' --kiss-pty ~/.ham-kiss
```

Choose `--audio-backend pulse` or `--audio-backend portaudio` to force a backend. Linux systems commonly expose
virtual audio cables through PipeWire or PulseAudio. For a temporary isolated loopback bench with PulseAudio:

```sh
pactl load-module module-null-sink sink_name=radio_a rate=8000 channels=1
pactl load-module module-null-sink sink_name=radio_b rate=8000 channels=1
```

Start station A with `--audio-in radio_b.monitor --audio-out radio_a`, and station B with
`--audio-in radio_a.monitor --audio-out radio_b`. This cross-connects their audio legs; remove each temporary sink
with `pactl unload-module MODULE_ID` when finished. Real virtual cable names vary by OS and audio server. Match the
modem sample rate to a supported rate; the PortAudio API may resample, while PulseAudio negotiates the requested
rate with its server.

The system audio leg uses mono signed 16-bit modem PCM and a bounded queue. Runtime shutdown logs queue drops,
overflows/underflows, and callback errors. Use radio VOX or a separate supported PTT control to key a transmitter;
this audio feature does not key PTT. Set speaker/mic levels so the radio neither clips nor applies aggressive voice
processing, and verify deviation and emissions on a suitable bench before transmitting. Software virtual-device
checks do not establish compatibility with a particular radio or a compliant on-air signal.

## Validation status

PortAudio callback behavior, PulseAudio capture-fd servicing, and CLI selection have unit coverage. The repeatable
cross-connect test creates and removes temporary PulseAudio null sinks:

```sh
.venv/bin/python lab/system_audio_loopback.py
```

On the development host, the test delivered one AX.25 UI frame with AFSK1200 and completed ARDOP 500MAX ARQ data
transfers in both directions at the CLI's default 8 kHz rate. Each ARDOP direction decoded a `ConAck500` and a
`DataACK`; measured modem times were 19.5 seconds each. All three tests reported zero RX/TX queue drops. This
establishes the audio leg, modem, and ARQ path through the local PulseAudio null-sink cross-connect.

The host's PortAudio library still exposes no hardware endpoints, so the successful runtime loopback used
PulseAudio/PipeWire-compatible CLI streams. Null sinks do not reproduce radio audio filtering, deviation, RF
fading/interference, acoustic/electrical mic-speaker levels, or PTT. No physical transceiver was available, and
these results do not establish on-air compatibility or regulatory compliance. See the dated
[loopback report](../lab/SYSTEM_AUDIO_LOOPBACK_2026_10_24.md) for the exact results and limits.
