#!/bin/bash
# Stop the policy: pause and hold the current pose (torque stays on).
#   ~/act-local/stop.sh         pause + hold
#   ~/act-local/stop.sh park    park the arms and exit the policy session
#   ~/act-local/stop.sh estop   cut torque immediately
if ! tmux has-session -t act-v3 2>/dev/null; then echo "no policy session running"; exit 0; fi
case "${1:-hold}" in
    park)  tmux send-keys -t act-v3 q; echo "parking the arms and exiting the policy session" ;;
    estop) tmux send-keys -t act-v3 e; echo "TORQUE CUT" ;;
    *)     tmux send-keys -t act-v3 Space; echo "policy paused; holding the current pose. go.sh restarts an attempt, stop.sh park parks." ;;
esac
