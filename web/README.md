# RealBot web

Realtor-facing frontend for RealBot: a library of SLAM-scanned spaces, an onboarding/pairing
flow for the bracketbot, and a 3D map viewer. Vite + React + TypeScript, Tailwind, Framer Motion,
Zustand. Design rules are in [`docs/TASTE.md`](docs/TASTE.md).

Current state: the library contains two preset SLAM maps; **Add a space** opens the `/onboard`
flow (4 instruction steps, simulated 2 s pairing → Start); `/map/:id` renders the selected grid
in 2D/3D and supports local mock waypoint markers. Pairing, robot state, and commands remain
simulated: the application is not connected to a physical robot or BracketBot Cloud.

Non-authoritative notes about the current prototype, the immediate hackathon relay, and a
possible later remote camera/control architecture are in
[`../docs/REMOTE_CONTROL_DIRECTION.md`](../docs/REMOTE_CONTROL_DIRECTION.md).
The observed robot-side SLAM topics, binary map packets, degraded states, and
live-telemetry integration requirements are documented in
[`../docs/SLAM_TELEMETRY.md`](../docs/SLAM_TELEMETRY.md).
The realtor-only post-scan workflow for following, pointing, spoken labeling,
narrated testing, and verified interactable overlays is specified in
[`../docs/INTERACTABLES_SETUP_PLAN.md`](../docs/INTERACTABLES_SETUP_PLAN.md).

## Run

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

## Hackathon remote-control demo

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

Then run `web/` and open `/`. The login portal offers two role-aware paths:

- **Visitor**: enter a tour access code (the relay room ID) to open `/user/:roomId`.
- **Realtor**: use `realtor@realbot.demo` / `demo` to open `/realtor`, then enter the operator
  dashboard with room, transport, pose, and command diagnostics.

The credentials can be changed with `VITE_REALTOR_EMAIL` and `VITE_REALTOR_PASSWORD`. This is
deliberately lightweight client-side access control for the hackathon prototype, not production
authentication. Visitors are redirected away from realtor-only routes; realtor sessions can switch
between management and the exact visitor experience.

The realtor dashboard's **Preview as user** link opens the real user route rather than a
separate mock. Both views receive the same camera frames and robot state. Clicking the live image
sends `move_to_view` with normalized image coordinates; `stop` and `use_action` use the same
acknowledged command channel. The SLAM map is realtor-only telemetry and is not a second steering
surface. Set `VITE_RELAY_WS_URL` when the relay is not on port 8000 of the same host.

## Maps

Every layout is a SLAM occupancy grid using the robot's `mapping.grid2d` encoding
(`0` unknown, `1` floor, `2` obstacle, `origin` + `resolution` in metres — see `src/lib/grid.ts`,
which mirrors `nav/main.py`). Presets are ROS `map.pgm` + `map.yaml` converted with:

```sh
npm run convert-map -- public/maps/<id> <id> "<Name>" "<credit>"
```

which writes `map.grid.json` and a styled `thumb.png`. Register the map in
`public/maps/_index.json`; the library grid lays out any number of entries.

Preset: `small-house` from
[aws-robotics/aws-robomaker-small-house-world](https://github.com/aws-robotics/aws-robomaker-small-house-world) (MIT).
