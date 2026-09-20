# Electrical box demo: init, go, stop

Three commands run the whole demo on robot 0188. Run them from any machine that can SSH to
the robot (the Jetson is `bracketbot@192.168.2.29`; key auth is set up from David's Mac).

| Command | What it does |
|---|---|
| `ssh bracketbot@192.168.2.29 '~/act-local/demo.sh'` | **Init (fresh).** Parks anything holding the arms, loads the policy (~25 s), homes, ramps to the demo start pose, runs one attempt. |
| `ssh bracketbot@192.168.2.29 '~/act-local/demo.sh guided'` | **Init (guided).** Starts Quest teleop and a pre-loaded policy waiting for handoff. |
| `ssh bracketbot@192.168.2.29 '~/act-local/go.sh'` | **Go.** Teleop running: kills it in place and starts the policy from that pose. Otherwise: ramps back to the demo start pose and runs a fresh attempt. `go.sh here` restarts from the current pose instead. |
| `ssh bracketbot@192.168.2.29 '~/act-local/new_start_2.sh'` | **Retry in place.** Restarts the attempt from wherever the arm is: no homing, no ramp back to the start pose. |
| `ssh bracketbot@192.168.2.29 '~/act-local/stop.sh'` | **Stop.** Pauses the policy and holds the pose, torque on. |
| `ssh bracketbot@192.168.2.29 '~/act-local/stop.sh park'` | Parks the arms and exits the policy session. Use at the end. |
| `ssh bracketbot@192.168.2.29 '~/act-local/stop.sh estop'` | Cuts torque immediately. |

## A typical session

1. Set the box up where the demonstrations had it and make sure nothing else is driving the
   arms (Quest teleop, the switch demo's control server, an old policy session).
2. `demo.sh` once. It takes about 40 s to load, home and ramp, then attempts the open.
3. `stop.sh` as soon as the door is open, before the arm starts wandering.
4. `go.sh` for each further attempt. The model stays loaded, so a retry costs only the
   4-second ramp back to the demo start pose.
5. `stop.sh park` when finished.

If the policy cannot find the latch hole on its own, use the guided flow: `demo.sh guided`,
put on the headset and drive the left finger into the hole, then `go.sh`. Once it starts
from there it does the flick by itself.

## Watching and intervening

On the robot, `tmux a -t act-v3` shows the live log. Keys inside the session:

| Key | Action |
|---|---|
| Space | pause and hold |
| R | restart the attempt from the current pose |
| N | ramp back to the demo start pose and restart (what `go.sh` sends) |
| E | cut torque (emergency) |
| Q | park the arms and exit |

Each second the log prints the measured joint state, the policy's raw target and the sent
command. The door-open motion is left J5 (the sixth value) climbing from near 0 to 70+.

## If something is off

- **"arms are still owned by another controller"**: another writer holds `arm_left.ctrl` /
  `arm_right.ctrl`. Stop teleop (Ctrl-C in its tmux window parks it) or the switch-demo
  control server, then retry.
- **"no policy session"** from `go.sh` or `stop.sh`: the session was killed. Run `demo.sh`.
- **Teleop died during `demo.sh guided`**: `tail ~/quest_teleop.log` on the robot, then run
  `demo.sh guided` again.
- **Policy stalls or wanders**: `stop.sh`, then `go.sh` for a clean attempt from the start
  pose. Fresh-start success is not 100%; the guided flow is the reliable fallback.
- **Model or scripts changed**: the deployed copy is `/home/bracketbot/act-local`. Copy
  updated scripts there; the weights (`run-v3/best/model.safetensors`) live only on the robot.

See `README.md` in this directory for the training pipeline and why the first attempt failed.

## Visitor action circles

The LiveKit visitor page can start this policy by clicking a saved **Electric box** circle.
It is enabled only on 0188 when the checkpoint is installed. Position the robot and arms
at the box first: this starts from the current pose, without navigation, homing, or a reset ramp.
Other action types stay disabled until they have a policy.

Deploy the updated `hand_tracking.py`, `visitor_livekit.py`, `visitor_actions.py`,
`act_local/live2.py`, and `act_local/visitor_control.py` with the visitor bridge.
It reuses `~/act-local` weights and Python environment. Existing ACT or Quest sessions must finish first; the click never
kills them. The managed session is `visitor-act`.

**Stop action**, Escape, leaving the page, or a lost connection pauses and holds the arms.
A later click starts a new attempt. An attempt also pauses after 120 seconds; the policy
does not detect that the door is open, so press Stop once it opens. WASD and Free Cam stay
locked while the policy owns the arms. At the end, an operator can park and exit with
`tmux send-keys -t visitor-act q`; only do this when the parking path is clear.
