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
7. **Later:** FX.25/IL2P FEC *(done)*, a P2P SIP direct-call mode *(done: `--sip-listen`)*, the TUI/dashboard, packaging (PyInstaller like El-Modem), and serialsim/com0com for real DCD lines. *(The last three are still open: see the roadmap below.)*

## Status (2026-10-22)

Phases 0–7 are done, and so is everything below; details are in the dated notes under `lab/`.

| Area | Done |
|---|---|
| Modems | AFSK 1200/300; G3RUH 9600/19200/38400; PAM-4 19.2k/57.6k; passband QAM 38.4k/51.2k |
| Link layer | AX.25 v2.0 connected mode; FX.25 (Direwolf-compatible); IL2P (Direwolf 1.7-compatible); erasure-aware FX.25 decoding; `--repeat` time diversity; CSMA/CD for rooms |
| DTE interfaces | KISS (pty/tty/TCP), TNC-2 command mode, AGWPE |
| Audio legs | SIP (PCMU/PCMA/L16 at 16 and 48 kHz, dial or listen, digest auth); IAX2 (µ-law, from El-Modem's binding); UDP raw PCM |
| Lab | Isolated Asterisk lab (`ham-modem-lab`, 172.31.73.0/24) with rooms 7300 (8 kHz), 7316 (16 kHz), 7348 (48 kHz) and echo 7399; ConfBridge harness; third-party test with Direwolf as a digipeater peer plus `kissutil` |

## Roadmap

Phases are ordered by value. The first is a prerequisite for every HF protocol after it.

8. **HF channel simulator** *(done 2026-10-22: `hammodem/hfchannel.py`, [lab note](../lab/HF_CHANNEL_2026_10_22.md))*. Over pure VoIP the channel is clean. HF modes matter for remote-HF gateways (a VoIP leg bridged to a real HF radio) and for interop with HF BBS and messaging networks, so they must be measured on realistic channels:
   - a Watterson / CCIR 520 (ITU-R F.1487) two-path fading model: good, moderate and poor channels, with Doppler spread, multipath delay, AWGN at a set SNR, frequency offset, and impulse noise;
   - available both offline (`tests/impair.py`, WAV tools) and live (an audio-hub mode that impairs the mix).
9. **Open HF/VHF modes that reuse what exists** *(Direwolf 2400 QPSK / 4800 8PSK and IL2P 300 done 2026-10-22, [lab note](../lab/PSK_IL2P300_2026_10_22.md); NinoTNC's own PSK modes still open -- find NinoTNC's published docs/firmware online)* (small):
   - Direwolf-compatible **2400 bd QPSK (V.26)** and **4800 bd 8PSK (V.27)**;
   - **IL2P at 300 bd** and NinoTNC's HF/VHF PSK modes;
   - Direwolf interop tests for each, like the earlier ones.
10. **ARDOP** (Winlink's open HF protocol; large). License checked 2026-10-22: **ardopcf is MIT** (Muething, Wiseman,
    LaRue; GPL-compatible, port with attribution; `/lib` libraries carry their own licenses). Reference: ardopcf
    (github.com/pflarue/ardop), `docs/refs/ARDOP_Specification_20171127.pdf` + Winlink host-interface spec. The
    spec gives frame types, bandwidths, payload/FEC sizes and ARQ rules; bit-level details (tones, carriers, RS,
    CRC init, call sign packing, session-ID hash) come from the source.
    - 10.0 **Reference peer**: ardopcf in a lab-network container, audio through ALSA FIFOs to the hub/ConfBridge
      (as with Direwolf): working ARDOP over VoIP immediately, and the live interop partner. Offline interop via
      its `--decodewav` / `--writetxwav`. *(Live peer done 2026-10-22: lab/ardop/run_peer.sh + relay.py +
      alsa_tap.c, UDP audio to the host; still open: bridging it into the hub/ConfBridge.)*
    - 10.1 **Native physical layer** *(done 2026-10-22, [lab note](../lab/ARDOP_PHY_2026_10_22.md))*: leader (50 bd
      two-tone), sync, frame-type 4FSK (10 symbols + parity), 4FSK data frames, RS FEC, CRC16, Packed6 call signs,
      session ID; verified both ways against ardopcf WAVs (our TX is sample-exact with ardopcf's).
    - 10.2 Multi-carrier 4PSK / 8PSK / 16QAM data frames (100 bd, 1-8 carriers) *(done 2026-10-22, same note;
      600 bd FM 4FSK frames, 12 kHz <-> 8/16/48 kHz resampling and a streaming receiver (`ardop/stream.py`; every
      mode passes 8 kHz G.711) done too; still open: better 16QAM.2000 noise margin)*.
    - 10.3 ARQ session layer (ConReq/ConAck timing, even/odd data, ACK/NAK quality, BREAK/IDLE/DISC/END, gear
      shifting) *(done 2026-10-22, [lab note](../lab/ARDOP_ARQ_2026_10_22.md): live sessions both ways with
      ardopcf at 500 and 2000 Hz; FEC mode, BUSY detection/BUSYBLOCK and the 10-minute ID done (see 10.4); still open: summation memory ARQ, better 16QAM.2000 noise margin)*.
    - 10.4 Winlink host interface (TCP 8515 commands / 8516 data) on our audio legs; test with Pat. *(done
      2026-10-22, [lab note](../lab/ARDOP_HOST_PAT_2026_10_22.md): `ham-modem --modem ardop`; Pat 1.0.0 P2P
      through ham-modem <-> ardopcf delivers messages both ways; across ConfBridge room 7300 at 8 kHz G.711 too;
      RTP loss 1-5% survives (lab note); FEC mode done, live against ardopcf both ways; IAX2 and third-station (monitor, carrier and noise jammer) runs done in the lab note.)*
11. **FreeDATA / codec2 OFDM data modes** (medium): wrap libcodec2 (LGPL) for the `datac*` modes rather than reimplement them, and add FreeDATA's ARQ/session layer if wanted. *(11.0 modem wrapper done 2026-10-22, [lab note](../lab/FREEDATA_CODEC2_2026_10_22.md): datac0/1/3/4/13 via ctypes, tests, SNR sweeps on AWGN/moderate/poor; 11.1 TNC modems `--modem datac*` done (AX.25 segmented over datac chunks, live in the ConfBridge room; datac4 suffers PBX timing slips); still open: FreeDATA ARQ/session layer, chunk-level combining of repeats)*
12. **Legacy HF ARQ modes** (medium): **PACTOR-I** (FSK 100/200 bd, memory-ARQ, Huffman), **AMTOR/SITOR-A/B** (CCIR 476/625) and **G-TOR** (Golay coding; Kantronics' DCC paper). All are published, and give legacy BBS interop. *(12.0 AMTOR/SITOR-A and -B done 2026-10-22, [lab note](../lab/AMTOR_2026_10_22.md): `--modem amtor`, built from ITU-R M.476-5, live in the ConfBridge room; 12.1 PACTOR-I done 2026-10-22, [lab note](../lab/PACTOR_2026_10_22.md): `--modem pactor`, CRC/Huffman/CS/status from hfterm GPL + public docs, FSK 100/200 bd, memory ARQ, text terminal, 24 tests + loopback 50/50 at 10 dB; G-TOR researched, not implemented: the public descriptions lack the header/ACK/table details needed to interoperate; selective FEC still open)*
13. **Published military HF standards** (large, optional): **STANAG 4285** and **MIL-STD-188-110A/B** serial-tone PSK, with **STANAG 5066** as the data-link layer. They are robust HF waveforms with public specifications.
14. **Keyboard / messaging modes** (medium each, lower TNC value): RTTY (Baudot), PSK31/63, MFSK16, Olivia/Contestia, Thor/DominoEX, and JS8. Interop targets: fldigi and JS8Call.
    *(RTTY done; PSK31/63 done; MFSK16 done; Olivia/Contestia done 2026-10-22: `--modem olivia`/`--modem contestia`,
    Walsh/Hadamard FEC, 32 tones 1000 Hz BW, 31.25 baud, Gray-coded MFSK, diagonal interleaver+scrambler, 9 tests;
    Thor/DominoEX done 2026-10-22: `--modem dominoex`/`--modem thor`, 18-tone IFK+ at 7.8125 baud (DomEX8/Thor8),
    nibble-based varicode (DominoEX) and MFSK varicode + R=1/2 K=7 FEC + diagonal interleaver (Thor), 9 tests;
    still open: JS8)*
15. **Reverse-engineered proprietary HF modes** (research). Method and constraints are below. Order:
    1. **PACTOR-II/III**: highest demand (Winlink, maritime); SCS has published protocol descriptions for III, and the II waveform is reasonably documented.
    2. **Clover-II/2000 (HAL)**: the waveform is well described in Ray Petit's DCC papers (pulsed four-tone, adaptive BPSK up to 16-level, Reed-Solomon) and the patents have expired. The ARQ handshake needs recordings of real links. Mostly historical value.
    3. **PACTOR-IV**: less documented.
    4. **VARA**: last. It is closed, changes often, and has no published spec.
16. **AX.25 kernel apps and NET/ROM** *(pinned by the user, NET/ROM especially)*. The kernel stack cannot run isolated in a container: it shares the host kernel, and as far as we know it only works in the initial network namespace. Container-friendly options:
    - a **userspace NET/ROM** layer (NODES routing, L3/L4 circuits, node commands; the default, as BPQ does it);
    - a **TUN device** for IP over AX.25 (PID 0xCC);
    - an **LD_PRELOAD shim** mapping AF_AX25 sockets onto AGWPE, for specific tools (`call`, `axcall`, `beacon`);
    - a **VM** with its own kernel when the real stack is needed.

    Before designing anything, ask the user which kernel-app and NET/ROM features to capture.
17. **Smaller open items:**
    - **Erasure handling:** erasure-aware IL2P decoding; an FX.25 tag search that tolerates erasures (losses that hit the 64-bit tag); handling loss on the station-to-bridge leg, which arrives as silence in the mix with no RTP gap.
    - **TNC-2:** settings persistence (JSON "NVRAM", like El-Modem) and multiple streams.
    - **IAX2 and SIP:** per-station IAX2 users so stations can call each other through the PBX; making Asterisk send 10 ms L16/48000 packets (20 ms packets are 1932 bytes and fragment); a QAM equaliser if a real network path adds filtering.
    - **Performance:** vectorised G3RUH PLL (38400 bd is the costliest mode left).
    - **Packaging and tooling:** packaging (PyInstaller), the TUI/dashboard, serialsim/com0com for real DCD lines, a connected-mode test against Direwolf through an AGW client.

### Reverse-engineering approach (phase 15)

- **Clean room, black box.** Work from published papers and protocol descriptions, and from over-the-air recordings (e.g. sigidwiki, or links recorded with real HAL or SCS hardware). Do not disassemble vendor software or firmware, and do not distribute vendor code.
- **Legal position.** Signals sent in the clear on amateur bands may be analysed. FCC §97.309 requires amateur digital codes to be publicly documented, and our implementation will be. Reverse engineering for interoperability is broadly protected (EU Software Directive Art. 6; US law is narrower). Check each vendor's licence terms (VARA's especially) in the user's jurisdiction before starting.
- **Naming.** Do not use vendor trademarks as mode names ("PACTOR" is SCS's, "VARA" is EA5HVK's); describe compatibility instead.
- **Prerequisites.** Real modems or link recordings are needed for the ARQ layers. Without them the waveform can be built, but handshake timing is guesswork. Open question for the user: is HAL/SCS hardware or recordings available?

### Decisions (user, 2026-10-22)

1. Order: channel simulator (8), then the small open modes (9), working up to ARDOP (10).
2. No HAL/SCS hardware or recordings are available: for phase 15, search online for recordings (sigidwiki and
   similar archives) and work from published papers.
3. NET/ROM / kernel-app scope (16): deferred; ask when we get there.

## Verification
- `python -m unittest discover tests` covers the codecs (KISS, AX.25, HDLC/NRZI/FCS), modem round-trips with impairments, and the CSMA logic.
- WAV benchmark: `tools/decode_wav.py wa8lmf_track2.wav` reports the frame count. Run Direwolf on the same file for a side-by-side comparison.
- Direwolf interop in both directions through WAV and stdin piping.
- Lab end-to-end: `cd lab/asterisk && docker compose up`, three `hammodem` containers in ConfBridge plus a Direwolf monitor, then a scripted `kissattach` + `beacon` + `axcall` session. Report a pass rate per loss level.
- Real-time margin: log per-block DSP time and confirm there are no RTP send-deadline misses over 10 minutes.
