#!/usr/bin/env bash
# (LEG=iax puts our TNC on an IAX2 leg instead of SIP.)
# ARDOP across the lab ConfBridge room 7300 (8 kHz G.711): ham-modem ARDOP TNC (SIP, PCMU/PCMA) and ardopcf
# (UDP -> sip_bridge.py -> SIP) in the same room, driven only through their host interfaces (live_hosts.py).
#   [THIRD_MODE=monitor|jam] [JAMARGS='--burst 2 --period 20'] lab/ardop/confbridge_ardop_test.sh [BYTES_A_TO_B] [BYTES_B_TO_A] [ARQBW] [ours|ardopcf]
# THIRD_MODE adds lab/ardop/room_third.py as a third station in the room (log: $OUT.third).
set -u
PBX=${PBX:-172.31.73.10}
ROOM=${ROOM:-7300}
cleanup() { docker stop ham-lab-ardopcf >/dev/null 2>&1; kill $TNC $BRIDGE $THIRD 2>/dev/null; }
THIRD=
trap cleanup EXIT
docker run -d --rm --name ham-lab-ardopcf --network ham-modem-lab --ip 172.31.73.40 -v "$PWD/lab/ardop:/cfg:ro" \
  ham-modem-lab/ardopcf:dev sh /cfg/run_peer.sh 172.31.73.1 7401 >/dev/null
PYTHONPATH=. .venv/bin/python lab/ardop/sip_bridge.py $ROOM@$PBX --bind 172.31.73.1 > "${OUT:-/dev/null}.bridge" 2>&1 &
BRIDGE=$!
if [ "${LEG:-sip}" = iax ]; then      # our TNC on an IAX2 (ulaw) leg; the secret stays in the git-ignored auth file
  export HAM_MODEM_IAX_SECRET=$(sed -n 's/^secret *= *//p' lab/asterisk/etc/iax_lab_auth.conf | head -1)
  LEGARGS="--iax-dial $ROOM@$PBX"
else
  LEGARGS="--sip-dial $ROOM@$PBX --local-ip 172.31.73.1"
fi
PYTHONPATH=. .venv/bin/python -m hammodem.main --modem ardop --mycall K1ABC --ardop-host 127.0.0.1:8515 --drive 50 \
  $LEGARGS -v > "${OUT:-/dev/null}.tnc" 2>&1 &
TNC=$!
if [ -n "${THIRD_MODE:-}" ]; then
  PYTHONPATH=. .venv/bin/python lab/ardop/room_third.py $ROOM@$PBX $THIRD_MODE --local-ip 172.31.73.1 ${JAMARGS:-} \
    > "${OUT:-/dev/null}.third" 2>&1 &
  THIRD=$!
fi
sleep 6
PYTHONPATH=. .venv/bin/python lab/ardop/live_hosts.py ${1:-1500} ${2:-800} ${3:-500MAX} ${4:-ours} --external-tnc
