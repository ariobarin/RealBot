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

## Panel buttons on the 0188 page

The stationary 0188 panel runs the same three scripts an operator would run over SSH, so the
browser and the terminal drive the demo identically. They are enabled only on 0188 with the
checkpoint installed, and nothing typed in the browser reaches a shell: the panel can ask for
these three fixed command lines and nothing else.

| Button | Runs | Notes |
|---|---|---|
| **Initialize (guided)** | `demo.sh guided` | Parks whatever holds the arms, starts Quest teleop, loads the policy, and waits for teleop's staged homing before reporting READY. Someone has to be at the robot wearing the headset for this to be useful, since its purpose is to let a person place the finger in the latch. |
| **Go** | `go.sh` | Hands off from teleop when it is running, otherwise ramps back to the demo start pose and runs a fresh attempt. Also opens the wrist camera. |
| **Stop** | `stop.sh` | Pauses the policy and holds the arms with torque on. Never disabled, so it is always the way out. |

The scripts run detached, because Initialize waits up to two minutes for homing, which is far
longer than an RPC. Each button reports the script's last printed line when it finishes. Go is
refused while another script is still running; Stop is not.

Deploy `hand_tracking.py`, `visitor_livekit.py`, `visitor_actions.py`, `act_local/live2.py`,
`act_local/visitor_control.py` and the three scripts with the visitor bridge. They reuse the
`~/act-local` weights and Python environment.

The policy cannot tell that the door opened and Go sets no run timeout, so press Stop once it
opens. A lost connection, leaving the page, or stale observations also pause and hold the arms.
WASD and Free Cam stay locked while the policy owns them. At the end, an operator parks and
exits with `~/act-local/stop.sh park`, and only when the parking path is clear.
