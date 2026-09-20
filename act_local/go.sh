#!/bin/bash
# Start or restart an attempt.
#   * Quest teleop running (after demo.sh guided): kill it WITHOUT parking (the arm daemon keeps holding
#     its last command) and start the pre-loaded policy from the current pose.
#   * Otherwise: reset -- ramp back to the demo start pose -- and run a fresh attempt from there.
#     Pass "here" to restart from the current pose instead of ramping back.
cd ~/act-local
if ! tmux has-session -t act-v3 2>/dev/null; then
    echo "no policy session: doing a cold start (demo.sh)"
    exec ~/act-local/demo.sh
fi
if pgrep -f 'quest_teleop/main.py' >/dev/null; then
    pkill -KILL -f 'quest_teleop/main.py'
    sleep 0.5
    tmux kill-session -t quest-teleop 2>/dev/null
    last=$(tail -1 live2.log)
    if grep -q "MODEL READY" <<<"$last"; then tmux send-keys -t act-v3 Enter; else tmux send-keys -t act-v3 Space; sleep 0.5; tmux send-keys -t act-v3 r; fi
    echo "handoff: policy running from the pose teleop left the arms in."
elif grep -q "MODEL READY" <<<"$(tail -1 live2.log)"; then
    tmux send-keys -t act-v3 Enter
    echo "policy was waiting at its prompt: started (homes + ramps unless it was launched with --hold-start)."
else
    key=n; [ "${1:-}" = here ] && key=r
    tmux send-keys -t act-v3 Space; sleep 0.5; tmux send-keys -t act-v3 "$key"
    if [ "$key" = n ]; then echo "reset: ramping back to the demo start pose, then a fresh attempt."; else echo "restarting from the current pose."; fi
fi
echo "watch:  tmux a -t act-v3      stop:  ~/act-local/stop.sh   (stop.sh park / stop.sh estop)"
