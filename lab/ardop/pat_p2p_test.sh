#!/usr/bin/env bash
# Winlink P2P end to end: Pat (K1ABC) -> ham-modem ARDOP TNC -> UDP audio -> ardopcf -> Pat (N0CALL), each Pat with
# a message for the other; one B2F session must deliver both. Everything on the isolated lab network
# (172.31.73.0/24, no host ports); winlink.org / getpat.io are mapped to 127.0.0.1 inside the Pat containers so
# nothing leaves the lab (Pat otherwise looks the callsign up at winlink.org before connecting).
#
#   lab/ardop/pat_p2p_test.sh [ARQBW] [RUNS]          (run from the repo root; images: lab/ardop, lab/pat)
set -u
BW=${1:-500MAX}
RUNS=${2:-1}
OUT=${OUT:-$(mktemp -d)}
NET=ham-modem-lab
BLOCK=(--add-host api.winlink.org:127.0.0.1 --add-host cms.winlink.org:127.0.0.1 --add-host server.winlink.org:127.0.0.1
       --add-host winlink.org:127.0.0.1 --add-host getpat.io:127.0.0.1 --add-host api.getpat.io:127.0.0.1)
# Pat skips its account prompt when the winlink.org API is unreachable and the mailbox is not empty.
SEED='M=~/.local/share/pat/mailbox/$CALL; mkdir -p $M/sent && cp $M/out/* $M/sent/'
sed "s/\"Max\": 500/\"Max\": ${BW%MAX}/" lab/pat/config_K1ABC.json > "$OUT/config_K1ABC.json"
sed "s/\"Max\": 500/\"Max\": ${BW%MAX}/" lab/pat/config_N0CALL.json > "$OUT/config_N0CALL.json"

cleanup() {
  docker stop ham-lab-pat-n0call ham-lab-pat-k1abc ham-lab-ardopcf >/dev/null 2>&1
  [ -n "${TNC:-}" ] && kill "$TNC" 2>/dev/null
}
trap cleanup EXIT

for run in $(seq 1 "$RUNS"); do
  docker run -d --rm --name ham-lab-ardopcf --network $NET --ip 172.31.73.40 -v "$PWD/lab/ardop:/cfg:ro" \
    ham-modem-lab/ardopcf:dev sh /cfg/run_peer.sh 172.31.73.1 7401 >/dev/null
  PYTHONPATH=. .venv/bin/python -m hammodem.main --modem ardop --rate 12000 --udp-audio 7401:172.31.73.40:7400 \
    --udp-bind 172.31.73.1 --ardop-host 172.31.73.1:8515 --mycall K1ABC --drive 50 -v > "$OUT/tnc_$run.log" 2>&1 &
  TNC=$!
  sleep 4
  docker run -d --rm --name ham-lab-pat-n0call --network $NET --ip 172.31.73.41 "${BLOCK[@]}" -e CALL=N0CALL \
    -v "$OUT:/cfg:ro" ham-modem-lab/pat:dev sh -c "echo 'Run $run: hello K1ABC from N0CALL via ardopcf. 73' |
      pat --config /cfg/config_N0CALL.json compose --p2p-only -s 'n0call run $run' K1ABC && $SEED &&
      exec pat --config /cfg/config_N0CALL.json -l ardop http" >/dev/null
  sleep 6
  start=$(date +%s)
  timeout 400 docker run --rm --name ham-lab-pat-k1abc --network $NET --ip 172.31.73.42 "${BLOCK[@]}" -e CALL=K1ABC \
    -v "$OUT:/cfg:ro" ham-modem-lab/pat:dev sh -c "echo 'Run $run: hello N0CALL from K1ABC via ham-modem. 73' |
      pat --config /cfg/config_K1ABC.json compose --p2p-only -s 'k1abc run $run' N0CALL && $SEED &&
      pat --config /cfg/config_K1ABC.json connect ardop:///N0CALL </dev/null 2>&1 | tr '\r' '\n' | grep -v '%\$';
      echo INBOX; cat ~/.local/share/pat/mailbox/K1ABC/in/* 2>/dev/null | grep -a '^Subject'" > "$OUT/k1abc_$run.out" 2>&1
  took=$(( $(date +%s) - start ))
  n0call_got=$(docker exec ham-lab-pat-n0call sh -c 'cat ~/.local/share/pat/mailbox/N0CALL/in/* 2>/dev/null' | grep -a -c "^Subject: k1abc run $run")
  k1abc_got=$(grep -a -c "^Subject: n0call run $run" "$OUT/k1abc_$run.out")
  disc=$(grep -a "host> DISCONNECT" "$OUT/tnc_$run.log" | head -1 | cut -c1-8)
  gone=$(grep -a "host< DISCONNECTED" "$OUT/tnc_$run.log" | head -1 | cut -c1-8)
  abort=$(grep -a -c "host> ABORT" "$OUT/tnc_$run.log")
  faults=$(grep -a -c "FAULT" "$OUT/tnc_$run.log")
  echo "RUN $run $BW: to N0CALL $n0call_got, to K1ABC $k1abc_got; session ${took}s; DISCONNECT $disc -> DISCONNECTED $gone; aborts $abort; faults $faults"
  cleanup
  TNC=
  sleep 2
done
echo "logs in $OUT"
