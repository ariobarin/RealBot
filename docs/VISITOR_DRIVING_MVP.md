# Visitor camera and click-to-drive MVP

> **Product priority (2026-09-19):** Connect the physical robot to the user-facing
> application. The MVP is live head-camera video, clicking a reachable floor
> location to drive there, and Stop. Realtor scanning/setup, following,
> gestures, speech labeling, and physical interactions are secondary.
> This scope takes precedence over the broader edge and interactables plans.

> **Single-user simplification:** [SINGLE_USER_DEMO.md](SINGLE_USER_DEMO.md)
> implements offline token generation and a manually started sole robot participant
> reusing the bbOS camera publisher. The hosted backend below is a future
> production requirement, not a blocker for the supervised development demo.
> Physical camera and driving acceptance are still pending.

## User experience

An authorized visitor opens the robot session and sees its real head-camera
feed. With the robot stationary and localization healthy, the visitor selects
**Choose destination**, then clicks clear floor in a fresh captured image.
This selection image is the rectified stereo-left image associated with depth,
not an arbitrary delayed frame from the head-camera stream. The robot validates
the target, drives there through bbOS navigation, and reports arrival or failure. Stop
remains available during navigation. Start with one destination at a time; a new
destination requires the previous route to finish or be stopped.

Camera viewing remains available if navigation is unavailable. A rejected click
explains why it cannot be used, without substituting invented coordinates.

## Existing foundation and missing connections

| Component | Current status | Remaining work |
| --- | --- | --- |
| Visitor camera-click UI | `LiveDrivePage` and `LiveDriveClient`: LiveKit head video, captured-image selection, normalized clicks, Stop and authoritative results | Authorized session/token backend and real media test |
| LiveKit viewer | Shared receiver, realtor read-only viewer, telemetry publisher | Install hooks in the existing robot participant; no second participant |
| Navigation | `BbosNavigationAdapter` writes `nav.command`, requires fresh route state and awaits arrival | Dedicated driving mode must release/skip legacy teleop writers; hardware verification |
| Click geometry | `BbosFloorView` reads aligned rect/depth/pose; `floor_goal` projects and checks fresh map, stationary pose, floor, range and clearance | Validate installed calibration, coordinate frames, timestamps and grid against measured physical targets with motion disabled |
| Command handling | `DrivingSession`: bound controller identity, session nonce, deadlines, duplicate handling, one-use captures, heartbeat watchdog and concurrent Stop | Bind identity from the trusted backend session record and integrate lifecycle into bbOS |

These foundations do not yet demonstrate physical click-to-drive.

## Local implementation and verification (2026-09-19)

The visitor routes `/user/:roomId` and `/control/:roomId` select LiveKit by
default. Set `VITE_ROBOT_TRANSPORT=relay` explicitly for the old local simulator.
`/user/:roomId/live` always selects the LiveKit page. The read-only realtor route
remains `/realtor/live/:roomId`. No route automatically starts a robot session.

Without `VITE_LIVEKIT_SESSION_ENDPOINT`, development builds offer manual
short-lived controller token entry. Production builds disable Connect until
configured. A local visitor session only opens the page; it does not grant
robot access. Secrets and device credentials never belong in Vite variables.

Install the `edge_agent[driving]` extra for NumPy/Pillow. Local checks:

```sh
cd edge_agent
python -m pytest
cd ../web
npm test
npm run build
npm run lint
npx playwright test e2e/live-drive.spec.ts e2e/live-telemetry.spec.ts e2e/control.spec.ts
```

Tests cover known floor projection and rotation, walls/depth edges/unknown
floor/clearance, stale map and moved robot, duplicate commands, unauthorized
senders, expired commands, Stop during startup, failed routes, heartbeat and
localization loss, prior-route arrival rejection, reconnect without command
replay, and browser image coordinates. Browser interaction tests use an explicitly
labeled UI fixture; protocol tests use fake rooms and navigation adapters.
They are not robot, calibration, latency, obstacle-avoidance or cross-network tests.
The robot remains stopped; no daemon deployment or hardware movement is part of
this local implementation.

## Three remaining integration gates

### A. Authorized session backend

The application endpoint must support this contract in addition to read-only
`mode: "view"` from [LIVEKIT_TELEMETRY_SETUP.md](LIVEKIT_TELEMETRY_SETUP.md):

```text
POST VITE_LIVEKIT_SESSION_ENDPOINT
Authorization: Bearer <realtor Supabase access token>   # when signed in
Content-Type: application/json

{"roomId":"requested-room","mode":"drive","accessCode":"visitor-invitation"}
```

The browser sends the access code only when there is no realtor bearer token.
Response: `{ "url": "wss://…", "token": "short-lived-token", "robotIdentity": "…" }`.

The server must validate realtor ownership or the visitor invitation/booking,
bind the allowed robot/room and a unique controller participant identity, reject
a second controller, and communicate that identity to the robot through its
trusted session service. Use room-scoped grants: subscribe=true, publish media=false,
publish data=true only for the authorized controller. Viewer data publication
remains disabled. Return no-store responses, restrict CORS, rate-limit invite
attempts, and expire/revoke the actual robot control session with the booking;
LiveKit token expiry alone must not be assumed to evict a connected participant.

This backend has **not** been implemented or located in RealBot. The existing
robot device token endpoint is not a browser authorization endpoint. Do not
invent signing credentials, reuse a device key in the browser, or grant control
based only on room ID or a participant-provided metadata field.

### B. Dedicated bbOS driving mode

Change the existing `remote_session` host in its own maintained source. In a
trusted driving session, skip its legacy `drive.ctrl`/arm writers and disable
movement/manipulation/quest input handlers for the whole session. Keep the
existing camera publisher and LiveKit participant. Then add the hook to the
host's supervised task list after connection:

```python
from edge_agent.driving import run_driving

# trusted_session is supplied by the backend, not by a browser data packet.
driving_task = asyncio.create_task(run_driving(
    room,
    controller_identity=trusted_session.controller_identity,
    legacy_teleop_disabled=True,
))
```

The flag is an integration assertion, not code that releases the old writers.
Propagate hook failure to session teardown, and cancel **and await** the hook
before closing the room. Teardown must stop navigation and close its writer.
Use the same cancellation path on participant disconnect, booking expiry,
revocation, robot shutdown and process errors. Never hot-switch a live legacy
teleop session into driving mode. No changes to `/home/bracketbot/bbos` have been
applied; this code cannot acquire control safely alongside its unchanged daemon.

### C. Operator-supervised acceptance

First prove video on different networks. Then verify installed bbOS topic and
calibration contracts, floor height origin, map axes and footprint radius with
motion disabled. Test measured floor points at several pixels/ranges before
permitting a short route. Finally exercise Stop, network loss, stale pose,
route failure and reconnection with a physical emergency-stop operator present.
The web Stop is a network command, not a physical emergency-stop replacement.

Mapping resets, calibration changes and SLAM restarts must terminate the driving
session and clear captures before allowing another selection. The local capture
revision includes calibration hash, pose-graph correction count and grid origin;
it is **not** a persistent map identity or guaranteed reset counter. Do not allow
map reset/export-import maintenance concurrently with visitor driving.

## Driving wire contract

- Controller → robot: reliable, destination-scoped `realbot.drive_command` JSON.
  Heartbeats contain `type=heartbeat`, `sessionId`, increasing integer `sequence`,
  and `expiresAt`. Commands contain `type=command`, unique `commandId`, `sessionId`,
  `expiresAt` and `action=capture|move|stop`. Move additionally includes
  `captureId,u,v`. No arbitrary map coordinates or velocity commands are accepted.
- Robot → controller: destination-scoped `realbot.driving` JSON with `version=1`,
  random `sessionId`, robot Unix `at`, and `type=state|capture|result`. State at
  5 Hz reports `ready,lease,busy,fault,canCapture`; capture includes `commandId`,
  `captureId`, base64 JPEG and `validForMs`; results include `commandId,status,detail`.
- Browser estimates robot time from each state, so synchronized wall clocks are
  unnecessary. Robot accepts command deadlines at most 5 s ahead; browser sends
  3 s commands and resends the same ID while awaiting acknowledgement. Controller
  heartbeat expires after 3 s, checked every 200 ms. Publish failure tears down
  control. These are software bounds, not verified motor stopping distances.
- Selection JPEG stays below 9 KB (typically 320×240), carried inside a packet
  below 15 KB. Native depth remains on robot. Capture expires after 10 s, is
  consumed once, and is cleared on Stop or readiness/lease loss. Goals must be
  within 0.25–2 m, within 12 cm of mapped floor height, on known floor with at
  least the configured nav footprint clearance. Current pose must match capture
  within 3 cm and 2°. These conservative initial thresholds require calibration
  validation on the actual robot; they are not validated physical guarantees.

## Implementation order

### 1. Real video in the visitor app

- Obtain an authorized visitor session through the application backend; retain
  accountless invitation access where applicable, with server-side room access
  checks. A room ID alone does not authorize hardware control.
- Reuse the existing bbOS LiveKit participant and `cam-wrist` publication,
  which is the head-camera feed in the audited implementation.
- Share the existing receive logic with the user-facing route. Show connection,
  camera availability, and localization readiness independently.
- Prove the camera works across separate robot/browser networks before enabling
  movement. The read-only session grants remain read-only until control is added.

### 2. LiveKit navigation and Stop

- Extend the existing `remote_session` with namespaced RealBot command/status
  handling and bind commands/heartbeats to the authorized controller identity.
  LiveKit data publication permission alone does not grant robot control.
- Verify that the remote-session drive writer releases ownership before bbOS
  navigation starts. Do not leave an idle teleoperation writer blocking nav.
- Wire the existing navigation adapter and command lifecycle to that handler.
- Ensure Stop can execute while navigation is awaiting completion; do not
  serialize it behind a long-running movement command.
- Verify a short known map-coordinate route, Stop, writer release, localization
  loss, controller disconnect, and reconnect without resumed motion.

### 3. Convert camera clicks into navigation targets

- Use a stationary robot for the first version. Permit clicks only with a fresh
  camera view, healthy localization, and no active route.
- Associate the clicked image with a retained aligned RGB/depth/pose capture and
  calibration/map revision. Sending only `u/v` against whichever depth frame is
  newest is insufficient. LiveKit video and data packets must not be assumed to
  arrive together; verify a frame association mechanism or use an explicit
  fresh captured-image selection step backed by a capture ID.
- Account for the actual video image rectangle, letterboxing/cropping, resolution,
  and raw-versus-rectified camera calibration when mapping the click to a ray.
- Resolve the ray with aligned depth, transform into the map frame, and require
  observed traversable floor with adequate clearance. Reject walls, furniture,
  missing/stale depth, incompatible map revisions, and out-of-range targets.
- Test projection against measured floor targets with motion disabled, then pass
  validated map-frame `x/y` to bbOS navigation for a supervised short route.
- Display pending, moving, arrived, stopped, or rejected/failed status based on
  authoritative robot results. A click marker alone is not evidence of movement.

## Required versus deferred

Required: live video, session authorization, healthy SLAM and usable navigation
grid, calibrated capture geometry, one controller, bounded commands, Stop,
disconnect handling, and accurate outcome feedback. SLAM and mapping run locally
through bbOS; an existing usable map or operator-guided mapping is sufficient.

Deferred: realtor setup UI, autonomous scanning, following, bare-hand gestures,
speech labeling/narration, interaction policies, saved interactables, camera dots
for interactions, arm Free Cam, full remote SLAM/voxel visualization, and rich
minimap presentation. Existing code for those features can remain; they do not
gate this MVP and must not be exposed as working physical controls.

## Acceptance gate

From the user-facing app on a different network, an authorized operator sees
real head video, clicks a visible floor target, and observes the robot arrive
with a matching result in the app. Stop interrupts motion; expired/duplicate
commands do not cause additional motion; invalid/stale clicks do not move it;
localization loss or controller disconnect stops the route; reconnect shows
current state without replaying a previous command. Completion requires this
hardware test as well as automated tests of geometry and session behavior.
