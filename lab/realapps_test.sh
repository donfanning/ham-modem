#!/usr/bin/env bash
# Real third-party packet software against ham-modem, all on the isolated lab network (no host ports):
#   * Direwolf 1.7 as an on-air peer (digipeater + beacon) on the audio hub
#   * Direwolf's kissutil as a KISS-TCP client of our TNC
# Pass: kissutil shows Direwolf's beacon and Direwolf's digipeated copy of the frame kissutil sent.
set -euo pipefail
cd "$(dirname "$0")/.."
GW=172.31.73.1 NET=ham-modem-lab HUB=17300 KISS=18001
LOG=${LOG:-/tmp/ham-realapps}; mkdir -p "$LOG"
export PYTHONPATH=$PWD
cleanup() { docker rm -f ham-lab-direwolf ham-lab-kissutil >/dev/null 2>&1 || true; kill $HUBPID $TNCPID 2>/dev/null || true; }
trap cleanup EXIT
.venv/bin/python tools/audio_hub.py --bind $GW --port $HUB > "$LOG/hub.log" 2>&1 & HUBPID=$!
.venv/bin/python -m hammodem.main --udp-audio 0:$GW:$HUB --kiss-tcp $GW:$KISS --txdelay 300 -m > "$LOG/tnc.log" 2>&1 & TNCPID=$!
APT='apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends direwolf python3 libasound2-plugins >/dev/null 2>&1'
docker run -d --rm --name ham-lab-direwolf --network $NET -v "$PWD/lab/realapps:/cfg:ro" debian:trixie-slim \
  bash -c "$APT; cp /cfg/asound.conf /etc/asound.conf; mkfifo -m 666 /tmp/dwout.fifo;
           python3 /cfg/relay.py $GW $HUB < /tmp/dwout.fifo & echo READY;
           cd /tmp && exec runuser -u nobody -- direwolf -c /cfg/direwolf.conf -t 0 -q hd > /tmp/dw.log 2>&1" >/dev/null
until docker logs ham-lab-direwolf 2>&1 | grep -q READY; do sleep 1; done
docker run --rm --name ham-lab-kissutil --network $NET debian:trixie-slim bash -c \
  "$APT; (sleep 6; echo 'N0CALL>APRS,WIDE1-1:>hello via the Direwolf digipeater'; sleep 40) | kissutil -h $GW -p $KISS" \
  > "$LOG/kissutil.log" 2>&1
docker exec ham-lab-direwolf cat /tmp/dw.log > "$LOG/direwolf.log" 2>/dev/null || true
echo "== kissutil (third-party KISS client on our TNC) received:"; grep -E "DW1TST|N0CALL" "$LOG/kissutil.log" | grep -v "^\s*$" || true
echo "== Direwolf heard / did:"; grep -E "N0CALL|DW1TST|Digipeater" "$LOG/direwolf.log" | head -8 || true
