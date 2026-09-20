# RealBot

Bracket Bot applications, imported from `/home/bracketbot/bbapps` on bracketbot-0187.
The original Bracket Bot MIT license is retained in `LICENSE`.

The working checkout on the robot stays at `/home/bracketbot/bbapps`.
Apps use the existing `/home/bracketbot/bbos` installation and `uv`;
this repository does not include the robot OS or its environment.

- `teleop.py`, `quest_teleop/`: keyboard and headset teleoperation
- `hand_tracking.py`: MediaPipe tracking for all 21 hand/finger landmarks from both head cameras
- `examples/`: hardware inspection examples
- `nav/`, `inference/`: navigation and policy inference
- `edge_agent/`: RealBot command/workflow orchestration plus thin adapters over authoritative bbOS services
- `relay/`: local protocol simulator and fake robot adapters; not the selected hardware transport
- `stereo_capture_web.py`: stereo calibration capture
- `greeter/`, `mimic/`, `play_sound/`, `low_battery/`: robot apps

Run an app from this directory with `uv run <script.py>` after checking its
hardware requirements. Teleoperation and movement apps command real hardware.
Credentials belong in local environment variables or ignored `.env` files.

## Hand tracking

`hand_tracking.py` reads the existing `camera.head.jpeg` topic without taking
camera ownership. It splits the stereo image, runs one MediaPipe tracker per
camera, and reports all 21 landmarks for up to two hands in each view.

```bash
uv run hand_tracking.py --port 8006
```

Open `http://bracketbot-0187.local:8006/` for the annotated stereo preview.
Machine-readable output is available at `/landmarks`, with concise runtime
telemetry at `/health`, metric stereo positions at `/stereo`, and the current
gesture classifications at `/gestures`, action-landmark state and records at
`/actions`, and the annotated image at `/frame.jpg`. Stereo output is expressed in a rectified
camera-pair frame with its origin midway between the 61.4609 mm-spaced optical
centres: X points right, Y down, and Z forward.

## Planning references

- [`docs/ACTION_LANDMARKING.md`](docs/ACTION_LANDMARKING.md) defines the current
  isolated, stationary thumbs-up/pointing/voice workflow for recording labelled
  SLAM-map action locations. It does not use person following or robot motion.
- [`docs/SINGLE_USER_DEMO.md`](docs/SINGLE_USER_DEMO.md) is the current practical
  connection path: private token generation, existing bbOS camera publisher,
  manually started camera/driving sessions, and no multi-user backend.
- [`docs/VISITOR_DRIVING_MVP.md`](docs/VISITOR_DRIVING_MVP.md) defines the current
  priority: real camera video and click-to-drive in the visitor app, with Stop.
  The local LiveKit UI, driving protocol and floor projection are implemented;
  the authorized session backend, bbOS host integration and supervised hardware
  acceptance remain. Realtor setup and physical interactions follow this milestone.
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
- [`docs/LIVEKIT_ARCHITECTURE.md`](docs/LIVEKIT_ARCHITECTURE.md) records the
  selected cross-network architecture: existing bbOS LiveKit transport, thin
  RealBot workflow adapters, and no competing production WebSocket relay.
- [`docs/REMOTE_CONTROL_DIRECTION.md`](docs/REMOTE_CONTROL_DIRECTION.md) records
  older non-authoritative working notes and tradeoffs. Where it discusses the
  WebSocket/JPEG relay as a deployment option, the LiveKit decision supersedes it.

## Web deployment

The React frontend in `web/` is configured for deployment on Vercel from the
repository root. Import this repository in Vercel and keep the project root at
the repository root; `vercel.json` installs and builds the frontend and routes
client-side URLs back to `index.html`.
