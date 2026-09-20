#!/bin/bash
# Retry from the current pose: no homing, no ramp back to the demo start pose.
# Pauses the policy (if running) and immediately restarts a fresh timed attempt from where the arm is.
cd ~/act-local
if ! tmux has-session -t act-v3 2>/dev/null; then
    echo "no policy session; run ~/act-local/demo.sh first"; exit 1
fi
if pgrep -f 'quest_teleop/main.py' >/dev/null; then
    echo "teleop is running; use ~/act-local/go.sh to hand off from it"; exit 1
fi
last=$(tail -1 live2.log)
if grep -q "MODEL READY" <<<"$last"; then
    echo "policy has not started yet; run ~/act-local/demo.sh or press Enter in tmux a -t act-v3"; exit 1
fi
tmux send-keys -t act-v3 Space; sleep 0.5; tmux send-keys -t act-v3 r
echo "restarted the attempt from the current pose (no homing, no ramp)."
echo "watch:  tmux a -t act-v3      stop:  ~/act-local/stop.sh"
