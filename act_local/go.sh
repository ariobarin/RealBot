#!/bin/bash
# Hand the arms from Quest teleop to the waiting ACT policy WITHOUT parking, or, if there is no
# teleop and the policy is paused, restart a timed run from the current pose.
cd ~/act-local
if pgrep -f 'quest_teleop/main.py' >/dev/null; then
    pkill -KILL -f 'quest_teleop/main.py'          # the arm daemon keeps holding teleop's last command
    sleep 0.5
    tmux kill-session -t quest-teleop 2>/dev/null
fi
if ! tmux has-session -t act-v3 2>/dev/null; then
    echo "no policy session; run ~/act-local/demo.sh guided first"; exit 1
fi
last=$(tail -1 live2.log)
if grep -q "MODEL READY" <<<"$last"; then
    tmux send-keys -t act-v3 Enter
    echo "policy started from the current pose."
elif grep -q "PAUSED" <<<"$last"; then
    tmux send-keys -t act-v3 r
    echo "policy restarted from the current pose."
else
    echo "policy is already running (last log line: ${last:0:60})"
fi
echo "watch it:  tmux a -t act-v3   (E = torque off, Space = pause, Q = park)"
