# RealBot web

Realtor-facing frontend for RealBot: a library of SLAM-scanned spaces, an onboarding/pairing
flow for the bracketbot, and a 3D map viewer. Vite + React + TypeScript, Tailwind, Framer Motion,
Zustand. Design rules are in [`docs/TASTE.md`](docs/TASTE.md).

Current state (Sprints 0–3): library page with the preset SLAM map; **Add a space** opens the
`/onboard` flow (4 instruction steps, simulated 2 s pairing → Start → preset map); `/map/:id` is
a placeholder.

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

Then run `web/` and use one of the two product views:

- `/connect` → `/user/:roomId` is the uncluttered end-user remote-tour experience.
- `/realtor/connect` → `/realtor/control/:roomId` is the operator dashboard with room,
  transport, pose, and command diagnostics.

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
