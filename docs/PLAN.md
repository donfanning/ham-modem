# Ham-Modem: AX.25 packet TNC over VoIP

## Context
El-Modem (`~/modem/software-modem`, Python, GPL-3.0+) is a working Hayes/V-series software modem that runs over SIP and IAX2. We want a sibling project in `~/ham-modem`: a standalone **software packet TNC**. Its "RF channel" is VoIP audio. The shared channel is an Asterisk ConfBridge/MeetMe room, and a direct peer-to-peer call works as a point-to-point link. Legacy packet applications reach it through serial ports and pty devices (KISS, TNC-2 command mode) and through KISS-TCP/AGWPE.

The modes come in this order: 1200 baud Bell 202 AFSK, then 300 baud HF AFSK, then 9600 baud G3RUH.

Decisions made:
- **Code reuse:** copy and slim the El-Modem pieces into a clean package. Do not import El-Modem or extract a shared library.
- **Language:** Python + numpy.
- **Interfaces:** KISS over serial/pty, TNC-2 command mode, and KISS-TCP + AGWPE.
- **Milestone 1:** 1200 baud AFSK through a ConfBridge.

## Research: ham packet modems vs. dialup modems

| Aspect | Dialup (El-Modem) | Ham packet (AX.25) |
|---|---|---|
| Channel | Point-to-point, full duplex | Shared, half duplex, many stations on one channel |
| Startup | V.8 answer tone, training, rate negotiation | None. The sender keys up and sends flags for TXDELAY; receivers must lock within a few flags |
| Link layer | V.42 LAPM / MNP over async bytes | AX.25 v2.0/2.2: synchronous HDLC with NRZI, bit stuffing and CRC-16 (X.25) FCS. UI frames (APRS) plus connected mode (SABM/UA/I/RR/REJ/DISC) |
| DTE interface | Hayes AT | KISS (FEND/FESC framing, TXDELAY/P/SLOTTIME/TXTAIL/FULLDUP parameters), TNC-2 `cmd:` mode, AGWPE TCP, 6PACK |
| Media access | Not applicable | p-persistent CSMA gated on DCD (carrier detect); PTT means transmitting audio |

### Modulations
1. **1200 baud Bell 202 AFSK.** Mark is 1200 Hz, space is 2200 Hz, phase-continuous, with NRZI (a 0 bit is a tone change). Fits within G.711 at 8 kHz. Real radios add pre- and de-emphasis, so the demodulator must tolerate ±6 dB "twist" between the two tones. Direwolf handles this with multiple slicers.
2. **300 baud HF AFSK.** 200 Hz shift, by default 1600/1800 Hz (configurable), same NRZI and HDLC as above. Fits easily in G.711.
3. **9600 baud G3RUH.** Baseband FSK: a self-synchronizing scrambler (1 + x¹² + x¹⁷), then NRZI, then a low-pass/raised-cosine shaped baseband signal. The receiver needs a flat response from about 0 to 6 kHz. **It cannot pass through 8 kHz G.711**, whose Nyquist limit is 4 kHz. It needs linear PCM at 16 kHz or more (L16/slin16; G.722 is ADPCM and lossy), and a ConfBridge mixing at 16 kHz or more.
4. **Later:** FX.25 and IL2P FEC (valuable with RTP loss), 2400 QPSK / 4800 8PSK (Direwolf-compatible), 19.2k G3RUH at 48 kHz.

### Prior art (algorithm references and interop targets)
- **Thomas Sailer's soundmodem, hdlcdrv and multimon.** The original sound-card AFSK and FSK modems.
- **Direwolf (WB2OSZ).** The reference: multi-slicer AFSK demodulators, G3RUH, FX.25/IL2P, KISS pty/TCP, AGWPE. **It is our primary interop and benchmark peer.** It can read and write audio through stdin/stdout or a UDP audio port, which lets the test harness feed it our VoIP audio.
- **multimon-ng.** A decode-only cross-check.
- **UZ7HO / QtSoundModem.**
- **WA8LMF "TNC Test CD" track 2.** The standard 1200 baud decode benchmark; Direwolf decodes about 1000 frames from it.

### What VoIP does to the signal
- **Codec:** use only PCMU/PCMA or L16. Opus, GSM and G.729 destroy FSK. Telephone band-pass (300–3400 Hz) is fine for AFSK.
- **Packet loss:** one 20 ms RTP gap is 24 bits at 1200 baud and kills the frame. El-Modem found that repeating the last frame for gaps of 5 frames or fewer keeps phase continuity (`modem_dce._gap_fill`). FEC (FX.25 or IL2P) is the real fix later.
- **ConfBridge as an RF channel:** the mixer sums all talkers, so collisions behave like RF collisions and CSMA+DCD maps directly. **However**, the bridge profile must disable denoise, AGC, the talk detector and DTX/silence suppression, use `mixing_interval=10|20`, and set `internal_sample_rate` (≥16000 for 9600 baud). Asterisk does not echo a participant's own audio back to them, so the modem never hears its own transmission. That makes it like half-duplex RF.
- **Jitter and clock drift (±100–250 ppm, per El-Modem notes):** the receive PLL handles this, as it handles crystal offset on RF. TXDELAY should default higher (300 ms) than typical RF values to cover jitter-buffer priming.

## Architecture (new package `~/ham-modem/hammodem/`)

```
DTE side                     TNC core                         Channel side
-----------------------      ----------------------------     ---------------------------
serial/ (pty, tty,      <->  kiss.py  (KISS codec)      <->   channel.py (CSMA/DCD, TX
  TCP, serialsim)            tnc2.py  (cmd: interpreter,        queue, TXDELAY/TXTAIL,
agw.py (AGWPE TCP)           AX.25 connected mode)              p-persist, slottime)
kiss_tcp.py                  ax25.py  (addr/ctl codec)          modems/ afsk.py (1200/300)
                             ax25_link.py (v2.2 state            g3ruh.py (9600)
                               machine: T1/T2/T3, N2)           hdlc.py  (NRZI+stuff+FCS)
                                                               audio/ rtp.py, sip/, iax/,
                                                                 g711.py, wav/stdin I/O
```

**Engine contract** (same pattern as El-Modem):
- `Modulator.frame_to_samples(bits) -> np.int16[]`
- `Demodulator.feed(np.int16[]) -> list[bytes]` (frames that pass FCS), plus `dcd: bool`

The audio clock is the RTP 20 ms tick. The TX thread pulls from the channel's sample queue and sends silence (not nothing) when idle, so the bridge never sees DTX gaps.

### Copy from El-Modem (slim, put under `hammodem/`, keep GPL-3.0+ headers)

| Source (`~/modem/software-modem/...`) | Becomes | Notes |
|---|---|---|
| `lab/asterisk/serial_port.py` (PtySerial, TcpSerial), `modem_serial.py`, `modem_serial_device.py`, `modem_serial_com.py` | `serial/` | Keep the backend interface: `fds/service/write/update_lines`. Map DCD to channel busy. |
| `lab/asterisk/sip_uas.py`, `sip_uac.py`, `sip_auth.py` | `audio/sip/` | Extend SDP to offer `PCMU`, `PCMA` and `L16/16000` (dynamic PT) |
| `lab/asterisk/iax_transport.py` + `native/iax` | `audio/iax/` | µ-law first; check slin16 support in the `iax2` crate later |
| RTP pieces of `OnlineAudioSession` in `lab/asterisk/modem_dce.py` (`_send_loop`, `handle_rtp_packet`, `_gap_fill`, probation) | `audio/rtp.py` | Lift these out of the 3.2k-line monolith; do not import it. Make the sample rate and frame size parameters. |
| `audio-peer/v42_lapm.py` `_Hdlc`, `_stuffed` | `modems/hdlc.py` | Add NRZI and a bit-level push from the demodulator |
| `audio-peer/mnp_link.py` `crc16_hdlc` | `modems/hdlc.py` | Same CRC as the AX.25 FCS |
| `audio-peer/v22.py` `Scrambler`/`Descrambler` | `modems/g3ruh.py` | Change taps to 1 + x¹² + x¹⁷ |
| `audio-peer/bell103.py` phase-continuous tone generator | `modems/afsk.py` TX | Synchronous NRZI bits instead of 8N1 |
| `audio-peer/v34_g711.py` (µ-law/A-law numpy) | `audio/g711.py` |  |
| `lab/asterisk/pcm_capture.py`, `audio-peer/lossy_sim.py` | `tools/` | Capture and impairment testing |
| `lab/asterisk/compose.yaml` + `asterisk/etc/asterisk/*` | `lab/asterisk/` | Add `confbridge.conf` and a dialplan room |

### New code
- **`modems/afsk.py` (receiver):** band-pass filter, then a mark/space quadrature correlator with the energy difference, then a low-pass filter, then a multi-slicer bank for twist tolerance. After that, a digital PLL for bit timing (Direwolf style: an integer phase accumulator that is nudged on transitions), then NRZI decode, then HDLC. Parameterize baud, mark and space so one class covers both 1200 and 300 baud.
- **`modems/g3ruh.py`:** a TX shaping filter and RX low-pass filter, a slicer, a PLL and a descrambler, at a 16 kHz or higher sample rate.
- **`ax25.py`:** address field, callsign/SSID, digipeater path, control field and PID encode/decode, plus a monitor-format pretty printer (TNC-2 `MONITOR` style).
- **`kiss.py`:** KISS framing and the command bytes 0x00–0x06 and 0xFF, with multi-port support (the port nibble selects the modem/channel).
- **`channel.py`:** p-persistent CSMA using DCD from the demodulator; TXDELAY flags, TXTAIL and full-duplex flag.
- **`tnc2.py`:** TNC-2 command subset:
  - `MYCALL`, `CONNECT`, `DISCONNE`, `CONVERSE`/`K`, `TRANS`
  - `MONITOR`, `UNPROTO`, `BEACON`
  - `TXDELAY`, `FRACK`, `MAXFRAME`, `PACLEN`, `RETRY`
  - `DISPLAY`, `RESTART`, `KISS ON`

  It needs **`ax25_link.py`**, the AX.25 v2.0 connected-mode state machine.
- **`agw.py` (AGWPE TCP, port 8000) and `kiss_tcp.py` (port 8001).**
- **`hammodem/main.py`:** command-line interface with these options:
  - `--modem afsk1200|afsk300|g3ruh9600`
  - `--sip-dial ext@host` / `--sip-listen` / `--iax-dial`
  - `--kiss-pty PATH`, `--kiss-tcp PORT`, `--agw PORT`, `--tnc2-pty PATH`, `--serial /dev/ttyS0`
  - `--audio-in/out wav|stdin|udp` for offline testing and Direwolf piping
  - `--mycall`

  Configuration lives in a TOML/JSON file. Logging uses the `logging` module, not prints.

## Phased plan
0. **Scaffold.** `git init` `~/ham-modem` with `pyproject.toml`, `hammodem/` and `tests/` (unittest, like El-Modem), plus LICENSE/NOTICE carried over and a README. Copy and slim the reused modules with their tests.
1. **Offline 1200 AFSK.** Build HDLC/NRZI/FCS, `afsk.py` TX/RX and `ax25.py`.
   - Round-trip tests run at 8 kHz with noise, twist and ±250 ppm clock drift through `lossy_sim`.
   - Decode the WA8LMF track 2 WAV (resampled to 8 kHz) and track the frame count against Direwolf.
   - Check cross-interop: Direwolf decodes our WAV output and we decode Direwolf's `gen_packets` output.
   - Profile so receive takes 5 ms or less per 20 ms block. Use numpy block processing and set the GC pause gate from El-Modem's lessons.
2. **KISS and channel.** Build `kiss.py`, `channel.py` (CSMA/DCD/TXDELAY) and the pty/TCP/serial backends. Test with `kissattach` (Linux AX.25), then `beacon`/`call`, and with Xastir or YAAC over KISS-TCP.
3. **VoIP transport plus ConfBridge (milestone 1).**
   - Build RTP, SIP and IAX with G.711.
   - Add the Asterisk lab with `confbridge.conf`: a `ham_room` bridge profile with `internal_sample_rate=8000` (16000 for 9600 baud later), `mixing_interval=20`, `dsp_drop_silence=no` and `jitterbuffer=no`. Add a user profile with `dsp_drop_silence=no`, `denoise=no`, `talk_detection_events=no` and `quiet=yes`, and an extension `7300 → ConfBridge(ham)`.
   - **Exit test:** three instances join the room. A and B exchange UI frames and a connected session; C monitors. A Direwolf instance fed the room audio (an AudioSocket/ExternalMedia leg or a fourth SIP client piping into `direwolf -r 8000 -`) decodes everything. Measure frame success under 1%, 3% and 5% loss and under jitter.
4. **TNC-2 command mode, AX.25 connected-mode state machine, and AGWPE.** Test with LinBPQ/JNOS over KISS and with a terminal over the TNC-2 pty (connect to a peer and chat).
5. **300 baud HF AFSK.** Same engine with new parameters; tests include ±10 Hz mistuning tolerance.
6. **9600 G3RUH.**
   - Add L16/16000 to SIP SDP, a 16 kHz RTP path and a wideband ConfBridge.
   - Test that G.711 is correctly refused or warned against.
   - Interop with Direwolf in 9600 mode.
   - Profile again, and add Numba or Rust only if the budget is exceeded.
7. **Later:** FX.25/IL2P FEC, a P2P SIP direct-call mode (a two-station link without a bridge), the TUI/dashboard, packaging (PyInstaller like El-Modem), and serialsim/com0com for real DCD lines.

## Verification
- `python -m unittest discover tests` covers the codecs (KISS, AX.25, HDLC/NRZI/FCS), modem round-trips with impairments, and the CSMA logic.
- WAV benchmark: `tools/decode_wav.py wa8lmf_track2.wav` reports the frame count. Run Direwolf on the same file for a side-by-side comparison.
- Direwolf interop in both directions through WAV and stdin piping.
- Lab end-to-end: `cd lab/asterisk && docker compose up`, three `hammodem` containers in ConfBridge plus a Direwolf monitor, then a scripted `kissattach` + `beacon` + `axcall` session. Report a pass rate per loss level.
- Real-time margin: log per-block DSP time and confirm there are no RTP send-deadline misses over 10 minutes.
