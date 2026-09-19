#!/usr/bin/env bash
set -euo pipefail

GPU=gpu
PORT=8080
CHECKPOINT=/home/bracketbot/ml/checkpoints/pi0_20260514_051732/checkpoints/005000/pretrained_model
INFERENCE_DIR=/home/bracketbot/bbapps/inference

SSH() { sshpass -p 1234 ssh "$@"; }

pkill -f "ssh.*-L *${PORT}:localhost:${PORT}" 2>/dev/null || true
sleep 0.3

if ! SSH -n "$GPU" "ss -tln 2>/dev/null | grep -q ':$PORT '"; then
    echo "[start] launching policy_server on $GPU"
    SSH -n "$GPU" "nohup setsid bash -c 'cd /home/bracketbot/ml/lerobot && exec /home/bracketbot/.local/bin/uv run python -m lerobot.async_inference.policy_server --host=0.0.0.0 --port=$PORT --fps=30' </dev/null >/tmp/policy_server.log 2>&1 &"
else
    echo "[skip] policy_server already listening on $GPU:$PORT"
fi

echo "[wait] policy_server port on $GPU"
ready=0
for _ in $(seq 1 120); do
    if SSH -n "$GPU" "ss -tln 2>/dev/null | grep -q ':$PORT '"; then
        ready=1
        break
    fi
    sleep 1
done
if [[ $ready -eq 0 ]]; then
    echo "[fail] policy_server didn't bind :$PORT on $GPU within 120s"
    echo "       check log: ssh $GPU tail -50 /tmp/policy_server.log"
    exit 1
fi

echo "[tunnel] forwarding $PORT -> $GPU:$PORT"
SSH -N -L "$PORT:localhost:$PORT" "$GPU" &
TUNNEL_PID=$!
trap "kill $TUNNEL_PID 2>/dev/null || true" EXIT

echo "[wait] local tunnel"
for _ in $(seq 1 20); do
    nc -z localhost "$PORT" 2>/dev/null && break
    sleep 0.5
done

cd "$INFERENCE_DIR"
POLICY_SERVER="localhost:$PORT" CHECKPOINT="$CHECKPOINT" uv run live_inference.py
