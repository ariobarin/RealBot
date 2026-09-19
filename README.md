# RealBot

Bracket Bot applications, imported from `/home/bracketbot/bbapps` on bracketbot-0187.
The original Bracket Bot MIT license is retained in `LICENSE`.

The working checkout on the robot stays at `/home/bracketbot/bbapps`.
Apps use the existing `/home/bracketbot/bbos` installation and `uv`;
this repository does not include the robot OS or its environment.

- `teleop.py`, `quest_teleop/`: keyboard and headset teleoperation
- `examples/`: hardware inspection examples
- `nav/`, `inference/`: navigation and policy inference
- `stereo_capture_web.py`: stereo calibration capture
- `greeter/`, `mimic/`, `play_sound/`, `low_battery/`: robot apps

Run an app from this directory with `uv run <script.py>` after checking its
hardware requirements. Teleoperation and movement apps command real hardware.
Credentials belong in local environment variables or ignored `.env` files.

## Planning references

- [`docs/REALTOR_GUEST_AUTH_PLAN.md`](docs/REALTOR_GUEST_AUTH_PLAN.md) records
  the implementation decision for Supabase-backed realtor accounts and
  accountless, invitation-based visitor access. It does not cover robot
  authentication.
- [`docs/MVP_EDGE_GAP_PATCHES.md`](docs/MVP_EDGE_GAP_PATCHES.md) turns the five
  cross-cutting MVP gaps into concrete runtime behavior, protocol changes, and
  acceptance checks.
- [`docs/INTERACTABLES_SETUP_PLAN.md`](docs/INTERACTABLES_SETUP_PLAN.md) defines
  the post-SLAM realtor workflow for following, bare-hand point capture, spoken
  labeling, narrated physical testing, persistence, and web visualization.
- [`docs/SLAM_TELEMETRY.md`](docs/SLAM_TELEMETRY.md) documents the observed
  bbOS SLAM, mapping, navigation, and browser telemetry contracts, including
  the live-map integration plan and degraded-state behavior.
- [`docs/REMOTE_CONTROL_DIRECTION.md`](docs/REMOTE_CONTROL_DIRECTION.md) records
  non-authoritative working notes about the current web prototype, the immediate
  hackathon relay, and possible later remote-control architecture. It is context
  for discussion, not an API contract, safety specification, or source of truth.

## Web deployment

The React frontend in `web/` is configured for deployment on Vercel from the
repository root. Import this repository in Vercel and keep the project root at
the repository root; `vercel.json` installs and builds the frontend and routes
client-side URLs back to `index.html`.
