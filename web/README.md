# RealBot web

Realtor-facing frontend for RealBot: a library of SLAM-scanned spaces, an onboarding/pairing
flow for the bracketbot, and a 3D map viewer. Vite + React + TypeScript, Tailwind, Framer Motion,
Zustand. Design rules are in [`docs/TASTE.md`](docs/TASTE.md).

Current state: the library contains two preset SLAM maps; **Add a space** opens the `/onboard`
flow (three steps with simulated 2 s pairing → Start scan → a simulated Scanning state); `/map/:id` renders the selected grid
in 2D/3D and supports local mock waypoint markers. Realtor sign-in uses Supabase Auth and requires
an organization membership row. Visitor room entry, pairing, robot state, and commands remain
hackathon-oriented or simulated rather than using the planned invitation/session boundary.

Non-authoritative notes about the current prototype, the immediate hackathon relay, and a
possible later remote camera/control architecture are in
[`../docs/REMOTE_CONTROL_DIRECTION.md`](../docs/REMOTE_CONTROL_DIRECTION.md).
The decided implementation direction for browser authentication is in
[`../docs/REALTOR_GUEST_AUTH_PLAN.md`](../docs/REALTOR_GUEST_AUTH_PLAN.md): Supabase Auth for
landlord/realtor accounts and accountless, expiring guest invitations for visitors. Robot
authentication is outside that plan.
The observed robot-side SLAM topics, binary map packets, degraded states, and
live-telemetry integration requirements are documented in
[`../docs/SLAM_TELEMETRY.md`](../docs/SLAM_TELEMETRY.md).
The realtor-only post-scan workflow for following, pointing, spoken labeling,
narrated testing, and verified interactable overlays is specified in
[`../docs/INTERACTABLES_SETUP_PLAN.md`](../docs/INTERACTABLES_SETUP_PLAN.md).

The selected real-hardware transport is LiveKit through bbOS `remote_session`;
see [`../docs/LIVEKIT_ARCHITECTURE.md`](../docs/LIVEKIT_ARCHITECTURE.md). The
current WebSocket client remains a simulator transport and does not yet connect
this application to robot hardware.

## Run

For the LiveKit visitor demo, set `VITE_ROBOT_TRANSPORT=livekit` in `.env.local`
and run `npm run dev -- --host 127.0.0.1 --port 5178`. Open `/user/0188`.
The left head camera, action dots, transparent SLAM map, and WASD use LiveKit;
no SSH tunnel is needed while viewing or driving.

The local Vite server reads `.realbot-demo/current/browser.session.json` from
the repository root. The matching robot credential goes in
`~/.config/realbot/visitor.session.json` on 0188. Issue these with the existing
`edge_agent.demo_tokens` CLI using Python 3.11, `--minutes 60 --enable-driving`,
and a private `--env-file`; never put the signing secret in a `VITE_` variable.
For renewal, generate into a fresh directory, replace both session files, restart
`visitor_livekit.py --config ~/.config/realbot/visitor.session.json`, and reload.
The bridge uses the installed bbOS remote-session Python environment via
`edge_agent/run_robot_demo.py`'s `runtime()` helper. Keep the legacy
`remote_session` daemon stopped and the annotated camera service on port 8006 running.

Tokens and the bridge expire after 60 minutes. Drive requires a fresh enable after
disconnect; stale commands, missing heartbeats, and browser focus loss stop it.
This localhost token handoff is a development demo, not production guest authorization.

For a local visitor session on 0188, keep this tunnel running:

```sh
ssh -NT -L 127.0.0.1:18006:127.0.0.1:8006 bracketbot-0188
```

Set `VITE_ROBOT_TRANSPORT=ssh` in `.env.local`, run
`npm run dev -- --host 127.0.0.1 --port 5178`, and join a tour from the home page.
The visitor page shows the left stereo camera with saved action markers.
Its read-only 3D SLAM panel refreshes once a second, previews up to 40,000 points,
and marks the robot's position. Drag to orbit, scroll to zoom, or right-drag to pan.
Click **Enable drive**, then hold WASD; Shift doubles the default 0.1 m/s forward
speed. Space, Escape, Stop driving, leaving the page, or losing focus stops drive.
After a disconnection, restore the tunnel and enable drive again.

This development-only endpoint accepts the local app origin and exclusively owns
`drive.ctrl` while enabled. It changes no base mode or arm controls. Commands arrive
every 50 ms; the socket releases control after 250 ms without a command, and 0188's
base daemon independently expires drive commands after 100 ms. The SSH fallback is
for this trusted local setup, not a public visitor authorization system.

Copy `.env.example` to `.env.local` and fill in the Supabase project URL and publishable key.
Do not put a Supabase secret/service-role key in `web/`.

```sh
cd web
npm install
npm run dev          # http://localhost:5173
```

```sh
npm run lint         # oxlint
npm run typecheck    # tsc -b
npm test             # vitest (grid maths, PGM parser)
npm run test:e2e     # playwright (first time: npx playwright install chromium)
npm run build
```

## Local simulated remote-control demo

Run the lightweight relay and simulator in separate terminals:

```sh
cd relay
uv sync
uv run uvicorn app:app --reload --port 8000
```

```sh
cd relay
uv run python simulator.py
```

Then run `web/` and open `/`. The app has two tabs:

- **Tours** (`/`): the public entry. Enter a tour access code (the relay room ID) to open
  `/user/:roomId`, or pick an open tour from the grid (a static list for now). Scheduled tours
  open a booking sheet; bookings are kept on the device and listed under **Your bookings**
  (`/bookings`), which a signed-in realtor sees from the other side. No account is needed.
- **Manage spaces** (`/realtor`): the realtor side, behind `/signin` — create or sign in to a
  verified Supabase account. Account creation also creates an organization and owner membership.
  From there open the operator dashboard with room, transport, pose, and command diagnostics.

Supabase setup, migrations, and first-owner account creation are documented in
[`../supabase/README.md`](../supabase/README.md). Playwright uses an explicit test-only auth mode;
there are no configurable demo realtor credentials in production builds. Visitors are redirected
away from realtor-only routes, while realtor sessions can switch between management and the exact
visitor experience.

The remaining migration replaces visitor-entered room IDs with realtor-created invitation links. A
visitor supplies a display name and, when configured, an optional contact email and separately
shared secret/PIN; this creates only a temporary tour-scoped guest session, not an account. Browser
WebSockets will require short-lived tickets after that session is authorized. Until the migration is
complete, neither the frontend route guard nor knowledge of a room ID provides a security boundary.

The realtor dashboard's **Preview as user** link opens the real user route rather than a
separate mock. Both views receive the same camera frames and robot state. Clicking the live image
sends `move_to_view` with normalized image coordinates; `stop` and `use_action` use the same
acknowledged command channel. The SLAM map is realtor-only telemetry and is not a second steering
surface. Set `VITE_RELAY_WS_URL` when the relay is not on port 8000 of the same host.

The visitor camera includes a read-only 2D minimap. Robot pose comes from `robot_state`; the client
also accepts an authoritative `navigation` telemetry message containing `status`, `goal`, `path`,
and optional `mapId` / `mapRevision`. All coordinates are map-frame metres. The frontend only
renders this data—it does not resolve camera clicks or calculate navigation paths.

Visitor controls also include a Free Cam mode for the left-hand camera. The frontend sends
`free_cam_start`, bounded absolute `free_cam_pose` targets, and `free_cam_stop`. Entering the mode
must be implemented atomically by the edge: stop and lock the mobile base, acquire the left arm,
and switch the outgoing video source to `camera.left.jpeg`. The browser never
publishes raw joint commands.

## Maps

Every layout is a SLAM occupancy grid using the robot's `mapping.grid2d` encoding
(`0` unknown, `1` floor, `2` obstacle, `origin` + `resolution` in metres — see `src/lib/grid.ts`,
which mirrors `nav/main.py`). Presets are ROS `map.pgm` + `map.yaml` converted with:

```sh
npm run convert-map -- public/maps/<id> <id> "<Name>" "<credit>"
```

which writes `map.grid.json` and a styled `thumb.png`. Register the map in
`public/maps/_index.json`; the library grid lays out any number of entries.

Bracketbot `mapping.voxels` NPZ exports can also be converted into a derived
occupancy grid, thumbnail, and full-colour 3D point cloud:

```sh
python scripts/convert-voxel-map.py scan.npz public/maps/my-scan my-scan "My scan"
```

Add the generated `cloud.bin` and `cloud.json` paths to the map's manifest entry
to render the original 3D scan instead of extruded occupancy-grid walls.

Preset: `small-house` from
[aws-robotics/aws-robomaker-small-house-world](https://github.com/aws-robotics/aws-robomaker-small-house-world) (MIT).
