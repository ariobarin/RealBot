# Single-user LiveKit demo

This is the active MVP connection path: one operator, one robot, manually
started sessions, no account/session backend. It replaces the requirement to
integrate with Bracket Bot's hosted session API for the supervised demo.

## What is implemented

- `edge_agent.demo_tokens`: offline generation of separate, room-scoped robot
  and browser tokens using your own LiveKit project credentials. Camera-only by
  default; explicit driving permission when requested. No network requests.
- `edge_agent.demo_session`: one robot participant, using the **installed bbOS
  `remote_session.session.publish_feed`** to publish the head camera as `cam-wrist`.
  A read-only JPEG reader uses the same head-camera left-eye split as bbOS.
- In drive mode, the same participant runs `run_driving`: capture ID + click →
  aligned depth/pose projection → `nav.command` → fresh `nav.state` results.
- The launcher never imports `remote_session.daemon`, opens manual drive/arm
  writers, polls bb-cloud, starts hardware daemons, or subscribes to incoming
  media. Legacy movement/Quest/manipulation packets have no handlers here.
- Controller departure, room disconnect/reconnect, expiry, camera failure,
  legacy-daemon re-enablement and SIGINT/SIGTERM all end the session. Navigation
  cleanup is awaited **before** room disconnect. No automatic restart/resume.
- The development visitor page can load `browser.session.json` locally to fill
  its connection form, without uploading the file or persisting the token.

The legacy remote-session daemon must stay stopped. This runner is an explicit
replacement for the duration of the demo, **not a second robot participant next
to it**. A Linux file lock prevents two copies of our runner; a `.stopped` check
and process scan reject the legacy daemon and are repeated during the session.
bbOS remains the final single-writer arbiter; other teleop tools must stay off.

## Verified versus still pending

Local tests exercise token signatures/grants, camera topic/splitting, camera-only
isolation, lifecycle failures, and the full session → command handler → fake
navigation → controller-disconnect cleanup path. These are not physical tests.

Read-only SSH inspection confirmed the installed camera publisher and LiveKit
1.1.9 room/options/event interfaces. The installed remote-session environment
has OpenCV/LiveKit but **does not have Pillow or PyYAML**, which the driving
capture adapter needs. No packages, daemon files, service markers, or robot
credentials were changed. No LiveKit connection or movement was initiated.
During inspection SLAM/depth/mapping/nav stop markers changed externally;
their operational state must be rechecked with the operator before testing.

Pending: your LiveKit project credentials, robot deployment/dependencies, real
cross-network video, measured floor projection, and supervised short driving.

## 1. Prepare camera-only credentials on your computer

Use an authorized LiveKit project you control; Bracket Bot's device API key is
not a LiveKit signing secret. Put these in a **private, Git-ignored `.env` file**:

```dotenv
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your-project-key
LIVEKIT_API_SECRET=your-project-secret
```

Do not use `VITE_` names. Do not copy the API secret to the robot or browser.
Use a Python 3.11+ development environment, from the RealBot repository root:

```sh
python -m pip install -e './edge_agent[driving,tokens]'
python -m edge_agent.demo_tokens --env-file .env --output .realbot-demo/camera-01
```

The two output files contain sensitive bearer tokens, not the signing secret:

- `robot.session.json`: copy privately to the robot, with owner-only permissions.
- `browser.session.json`: keep on your computer and load into the development UI.

Each invocation creates a fresh room and refuses to overwrite an output folder.
The participant names are fixed (`realbot-robot`, `realbot-controller`), but the
room gets a unique suffix to isolate earlier demo credentials. Default lifetime
is 30 minutes; `--minutes` accepts 1–60. The robot enforces a matching session
deadline because token expiry alone does not evict an existing connection.

These files and `.realbot-demo/` are Git-ignored. Linux files are created mode
600; on Windows they inherit the private user folder's access controls. Never
put them under `web/public`, in a URL, or in a committed config.

## 2. Prepare the robot without starting a session

Deploy the reviewed `edge_agent` source through the normal repository workflow,
preserving unrelated robot changes. The following examples assume the checkout
is `/home/bracketbot/bbapps`. They have **not** been run as part of this work.

The launcher uses the installed remote-session Python and selected entries from
its cached bbOS Nix environment. It does not run `devenv` shell hooks or dependency
sync; bare `.venv/bin/python` lacks the required native library paths on this robot.

```sh
cd /home/bracketbot/bbapps
python3 edge_agent/run_robot_demo.py --check
```

This checks imports and the legacy-daemon guard only: no rooms, readers, writers,
or services. It is not proof that camera/SLAM/nav services are healthy.

For later driving, install **Pillow and PyYAML only** into the existing
remote-session environment through its normal dependency-management process.
Do not upgrade/reinstall bbOS, NumPy, OpenCV or LiveKit for this feature. Record
those extra dependencies in the maintained bbOS environment so a later sync does
not remove them. Then `--check --enable-driving` also checks the driving imports.
Camera-only operation does not require those extra packages.

## 3. Camera-only acceptance (operator present)

First confirm legacy `remote_session` is stopped and no other remote-control
process is active. Coordinate camera startup with the operator; this launcher
does not start it. Put the robot credential outside the repository, e.g.
`/home/bracketbot/.config/realbot/robot.session.json`, with mode 600.

```sh
python3 edge_agent/run_robot_demo.py --config /home/bracketbot/.config/realbot/robot.session.json
```

On your computer, run the web development server with
`VITE_LIVEKIT_SESSION_ENDPOINT` unset, open `/user/demo-bot/live` through the
normal visitor/realtor entry, choose **Load demo session**, select
`browser.session.json`, then Connect. The route's `demo-bot` label is not a room
credential; the signed token selects the actual room. Camera-only tokens permit
viewing and keep navigation controls unavailable. Test video across networks.

Disconnect the browser or press Ctrl+C on the robot to end this one-shot session.
If the camera goes missing/stale for about 3 seconds, the runner ends rather than
leaving control attached to a frozen feed. This is not a validated stopping-time
or stopping-distance guarantee.

Manual credential loading is **development-only**. Production builds still
require an authenticated token endpoint; this work does not add an unsecured
public token endpoint or embed a permanent controller token in the website.

## 4. Click-to-drive acceptance (separate supervised test)

Generate a new pair with `--enable-driving` into a new output folder, copy its
robot file privately, and load its matching browser file. Only after the operator
has approved motion, prepared SLAM/depth/mapping/nav, verified calibration and
cleared a short route, launch with both the drive config and explicit flag:

```sh
python3 edge_agent/run_robot_demo.py --config /home/bracketbot/.config/realbot/robot.session.json --enable-driving
```

Wait for real head video and healthy localization. Choose destination captures
the depth-aligned rectified stereo image; click nearby clear floor. Verify the
projected coordinates against measured points with motion disabled before the
first actual drive. Then verify arrival/failure, Stop, controller disconnect,
camera loss, localization loss and fresh-session reconnect. Have a physical
emergency-stop operator present. No map reset or calibration changes during a
visitor session. See [VISITOR_DRIVING_MVP.md](VISITOR_DRIVING_MVP.md) for geometry
limits and the remaining physical acceptance criteria.

## References

[LiveKit token grants](https://docs.livekit.io/frontends/reference/tokens-grants/)
and [Python room lifecycle](https://docs.livekit.io/reference/python/livekit/rtc/room.html).
