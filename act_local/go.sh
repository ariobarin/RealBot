#!/bin/bash
# Hand the arms from Quest teleop to the waiting ACT policy WITHOUT parking:
# kill teleop outright (the arm daemon keeps holding its last command), then press Enter for the policy.
pkill -KILL -f 'quest_teleop/main.py'
sleep 0.5
tmux kill-session -t quest-teleop 2>/dev/null
tmux send-keys -t act-v3 Enter
echo "policy started from the current pose. watch it:  tmux a -t act-v3   (E = torque off, Space = pause, Q = park)"
