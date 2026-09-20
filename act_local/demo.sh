#!/bin/bash
# One-shot electrical-box demo on robot 0188.
#   ~/act-local/demo.sh          fresh run: home, ramp to the demo start pose, run the ACT policy (120 s)
#   ~/act-local/demo.sh guided   Quest teleop + a pre-loaded policy; guide the finger into the hole, then run ~/act-local/go.sh
# Watch a run:  tmux a -t act-v3     (E = torque off, Space = pause, R = restart, Q = park + exit)
set -u
cd ~/act-local
mode=${1:-fresh}
if tmux has-session -t act-v3 2>/dev/null; then
    if tail -1 live2.log | grep -q "MODEL READY"; then
        tmux kill-session -t act-v3          # never took the arms: nothing to park
    else
        tmux send-keys -t act-v3 q
        for i in $(seq 1 60); do tmux has-session -t act-v3 2>/dev/null || break; sleep 1; done
    fi
fi
if tmux has-session -t quest-teleop 2>/dev/null; then
    tmux send-keys -t quest-teleop C-c
    for i in $(seq 1 45); do tmux has-session -t quest-teleop 2>/dev/null || break; sleep 1; done
fi
tmux kill-session -t act-v3 2>/dev/null
tmux kill-session -t quest-teleop 2>/dev/null
if .venv/bin/python arm_owner.py | grep -q OWNED; then
    echo "arms are still owned by another controller; stop it first:"; .venv/bin/python arm_owner.py; exit 1
fi
echo "=== demo.sh $mode $(date +%T) ===" >> live2.log
if [ "$mode" = guided ]; then
    tmux new -d -s act-v3 ".venv/bin/python -u live2.py --hold-start 2>&1 | tee -a live2.log"
    tmux new -d -s quest-teleop "cd ~/realbot && /home/bracketbot/.local/bin/uv run quest_teleop/main.py 2>&1 | tee ~/quest_teleop.log"
    echo "teleop starting, policy loading (~25 s). Guide the finger into the hole, then run:  ~/act-local/go.sh"
else
    tmux new -d -s act-v3 ".venv/bin/python -u live2.py --yes 2>&1 | tee -a live2.log"
    echo "policy starting: loads (~25 s), homes, ramps to the demo start pose, runs 120 s.  Watch:  tmux a -t act-v3"
fi
