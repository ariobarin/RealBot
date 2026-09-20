# Camera-pose Free Cam

A standalone right-hand camera page using the installed bbOS Rust IK solver.
It is separate from the LiveKit tour UI.

Start takes exclusive right-arm and stopped-base control, then slowly moves the
arm to a forward viewing pose. Hold arrows, WASD, or a direction button to look
around. Release freezes the target; Look forward recenters. The shoulder,
elbow, forearm, wrist and lift cooperate. The gripper and left arm stay held
and untouched respectively. The base retains its configured balance mode.
Open claw widens the right gripper using bbOS's 1.2-radian open setting while
holding the camera pose. It ramps at 0.08 motor turns/s; Stop freezes it, and
subsequent camera movements retain its opening.

The camera target has zero roll relative to the upright robot. Pan requests
span +/-110 degrees and tilt spans -90/+85 degrees. The camera follows a 12 cm
radius sweep in front of the right side, centered 20 cm right of the arm base.
The lift may move within 8 cm of its starting height. Every IK solution must
fit the actual calibrated joint limits, stay within 25 mm and 3 degrees of its
target, and remain continuous with the preceding solution. Unreachable views
hold the last valid target and display Reach limit. These are reach bounds,
not a full link collision model or gravity compensation during base lean.

The bbOS chain is J0 lift, J1 shoulder knuckle, J2 upper arm, J3 elbow,
J4 forearm rotation, J5 wrist knuckle, J6 hand rotation, J7 gripper.
The camera mount on 0187 is modeled as optical right = EEF Y, down = -EEF X,
forward = EEF Z, based on the observed wrist-camera response. The sweep uses
the EEF position; the lens offset has not been calibrated. Do not treat the
URDF camera housing frame as the optical frame or transfer this mount mapping
to another robot without checking its image.

Native right-arm IK path checks on 0187 reached -90/+85 tilt at zero pan,
about -87/+91 pan at zero tilt, and (-80,-80)/(60,-80) diagonal views. These
are model results, not a rectangular guarantee at every combined angle.
Live right-arm checks reached the forward view and 60 degrees down with the
claw open. Camera frames confirmed an upright forward view and the floor;
the controller completed both moves and held without a fault.
Camera requests ramp to 20 degrees/s; rotary targets stay below 20 degrees/s,
lift targets below 0.1 motor turns/s. Initial positioning uses half those
speeds. IK reaches a workspace edge before the requested angular limit when
necessary.

Missing jog updates for 350 ms freeze the target. Stop, a one-second browser
heartbeat expiry, stale camera/state, excessive tracking error, load or base
motion stop the trajectory while keeping torque on. Video and heartbeats use
separate loops. Release cuts right-arm torque and requires physical support.
SIGINT/SIGTERM while holding stops movement and waits for Release. Process or
power failure cannot guarantee a hold; bbOS protections remain authoritative.

```sh
python3 edge_agent/run_free_cam.py --robot bracketbot-0187 --control
ssh -N -L 8012:127.0.0.1:8012 bracketbot
```

Open the loopback URL printed by the process, including its private fragment
key. Without --control the page is read-only. It creates no actuator writers
before Start, refuses competing Quest/inference/navigation owners, and does
not change daemon configuration. Its private IK model substitutes calibrated
limits without changing the installed URDF or the shared bbOS solver.


## Local tour button

The visitor view opens the existing right-hand controller in a modal through
FreeCamButton. Opening it stops/disconnects tour driving; closing the panel
leaves driving stopped. The embedded controller retains its own heartbeat,
Stop and supported torque-release controls. Closing holds the arm rather than
releasing torque or automatically resuming navigation.

For the local demo, Vite serves a private, room-bound connection from the
ignored `.realbot-demo/freecam.session.json`: `{ "roomId": "0187", "url":
"http://127.0.0.1:8012/#<current-controller-key>" }`. Refresh this private file
when the controller restarts. The key is never included in the web build.
The running tour preview at port 5178 has the same component in SshCameraPage.
Hosted tour sessions still need a deployed transport; this local connection
must not be presented as remote LiveKit Free Cam support.
