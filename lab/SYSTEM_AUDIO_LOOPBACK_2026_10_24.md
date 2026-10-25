# System audio modem loopback — 2026-10-24

## Setup

- Host audio server: PulseAudio 17.0.
- The lab script created temporary, mono PulseAudio null sinks at 8 kHz and removed them at exit.
- The two station paths were cross-connected: station A playback to station B capture monitor, and vice versa.
- Command: `.venv/bin/python lab/system_audio_loopback.py --timeout 35`.
- This ran the actual `PulseAudio` audio leg, modem implementations, and the same 20 ms audio clocking pattern used by
  the application runtime. It did not use UDP/SIP audio as the transport.

## Results

| Check | Result | Evidence |
| --- | --- | --- |
| AFSK1200 AX.25 UI frame | Pass | Receiver decoded the expected frame; one frame heard; 37 audio blocks serviced. |
| ARDOP 500MAX, N0CALL → K1ABC | Pass | Expected payload received; `ConAck500` and `DataACK q21` decoded; 19.44 modem seconds. |
| ARDOP 500MAX, K1ABC → N0CALL | Pass | Expected payload received; `ConAck500` and `DataACK q21` decoded; 19.44 modem seconds. |
| Audio queues | Pass | All RX/TX queue-drop counters were zero in all three checks. |

The script finished with `PASS`. A prior investigation found capture was not serviced reliably by the initial reader
thread design. PulseAudio capture now uses its file descriptor in the runtime's `select()` loop; a unit test covers
sample framing through that path.

## Limits

This validates software PCM transport and modem operation through a local audio-server cross-connect. Null sinks do
not reproduce transceiver audio response, filtering, deviation, PTT/keying, levels, RF propagation, fading, or
interference. No physical radio or PortAudio hardware endpoint was available, so no radio-path or on-air claim is
made. Use VOX or separate PTT control for a radio and verify levels/deviation on a suitable bench before RF use.

Repeat with `.venv/bin/python lab/system_audio_loopback.py`. Setup details and device selection are in
[`AUDIO_DEVICES.md`](../docs/AUDIO_DEVICES.md).
