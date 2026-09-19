# RealBot web

Realtor-facing frontend for RealBot: a library of SLAM-scanned spaces, an onboarding/pairing
flow for the bracketbot, and a 3D map viewer. Vite + React + TypeScript, Tailwind, Framer Motion,
Zustand. Design rules are in [`docs/TASTE.md`](docs/TASTE.md).

Current state (Sprints 0–2): library page with the preset SLAM map and an inert **Add a space**
card; `/onboard` and `/map/:id` are placeholders.

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
