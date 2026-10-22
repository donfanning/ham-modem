#!/bin/sh
# Container entry point: ardopcf as a live ARDOP peer on the lab network, audio as UDP via relay.py.
#   run_peer.sh PEER_HOST PEER_PORT [ardopcf args...]
set -e
cp /cfg/asound.conf ~/.asoundrc
gcc -shared -fPIC -o /tmp/alsa_tap.so /cfg/alsa_tap.c -ldl -lasound
python3 /cfg/relay.py 7400 "$1" "$2" &
sleep 1
shift 2
LD_PRELOAD=/tmp/alsa_tap.so exec ardopcf "$@" 8515 ardopin ardopout
