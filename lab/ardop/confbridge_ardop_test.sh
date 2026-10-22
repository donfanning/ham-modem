#!/usr/bin/env bash
# ARDOP across the lab ConfBridge room 7300 (8 kHz G.711): ham-modem ARDOP TNC (SIP, PCMU/PCMA) and ardopcf
# (UDP -> sip_bridge.py -> SIP) in the same room, driven only through their host interfaces (live_hosts.py).
#   lab/ardop/confbridge_ardop_test.sh [BYTES_A_TO_B] [BYTES_B_TO_A] [ARQBW] [ours|ardopcf]
set -u
PBX=${PBX:-172.31.73.10}
ROOM=${ROOM:-7300}
cleanup() { docker stop ham-lab-ardopcf >/dev/null 2>&1; kill $TNC $BRIDGE 2>/dev/null; }
trap cleanup EXIT
docker run -d --rm --name ham-lab-ardopcf --network ham-modem-lab --ip 172.31.73.40 -v "$PWD/lab/ardop:/cfg:ro" \
  ham-modem-lab/ardopcf:dev sh /cfg/run_peer.sh 172.31.73.1 7401 >/dev/null
PYTHONPATH=. .venv/bin/python lab/ardop/sip_bridge.py $ROOM@$PBX --bind 172.31.73.1 > "${OUT:-/dev/null}.bridge" 2>&1 &
BRIDGE=$!
PYTHONPATH=. .venv/bin/python -m hammodem.main --modem ardop --mycall K1ABC --ardop-host 127.0.0.1:8515 --drive 50 \
  --sip-dial $ROOM@$PBX --local-ip 172.31.73.1 -v > "${OUT:-/dev/null}.tnc" 2>&1 &
TNC=$!
sleep 6
PYTHONPATH=. .venv/bin/python lab/ardop/live_hosts.py ${1:-1500} ${2:-800} ${3:-500MAX} ${4:-ours} --external-tnc
