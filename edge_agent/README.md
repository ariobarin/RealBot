# RealBot orchestration and bbOS adapter foundation

This package contains RealBot-specific command and workflow orchestration. It
is intentionally independent from FastAPI and browser code so a thin bbOS
adapter and `relay/simulator.py` can exercise the same protocol behavior.

The deployed bbOS interfaces were inspected read-only on `bracketbot-0187` on
2026-09-19. bbOS remains authoritative for IPC ownership, navigation, sensor
alignment, SLAM health, command freshness, base safety, and motor control.
`adapters/bbos_navigation.py` is the first thin adapter; it has not yet been
launched against moving hardware.

For the current one-user MVP, see
[`../docs/SINGLE_USER_DEMO.md`](../docs/SINGLE_USER_DEMO.md): offline token
generation, camera-only startup, and explicit opt-in click-to-drive using the
installed bbOS LiveKit publisher. No hosted session backend is needed for that
development flow; robot deployment and physical acceptance remain pending.

## Implementation boundary

| Capability | Owner | This package does |
| --- | --- | --- |
| SLAM, maps, pose health | bbOS `slam` and `mapping` daemons | Read and normalize |
| Path planning and arrival | bbOS `nav.command`, `nav.state`, `nav.plan` | Submit a route and await terminal state |
| Base command freshness and limits | bbOS `base` daemon and firmware | Never bypass them |
| Sensor timestamp alignment | bbOS `Reader(aligned_to=...)` | Validate and package the aligned samples |
| Remote viewer/controller policy | Existing LiveKit session plus RealBot orchestration | Lease, validate, acknowledge, deduplicate |
| Interactables setup | RealBot | Coordinate adapters and verification |

The robot also contains a bbOS `remote_session` daemon with LiveKit camera,
microphone, browser/Quest teleoperation, disconnect teardown, and arm parking.
RealBot will extend that transport. The WebSocket relay is retained only for
local simulation and protocol tests; it must not be deployed as a competing
camera or hardware-control path. See
[`../docs/LIVEKIT_ARCHITECTURE.md`](../docs/LIVEKIT_ARCHITECTURE.md).

## Responsibility split

```text
web:        joins LiveKit; sends high-level commands; renders authoritative state
LiveKit:    carries media plus namespaced realtime command/state data
edge agent: validates commands and coordinates RealBot workflow modes
adapters:   translate intent into audited bbOS topics and await bbOS state
bbOS:       owns navigation, IPC writers, sensor alignment, local safety, hardware
```

LiveKit is not a safety boundary. The RealBot lease is an additional session
guard layered over bbOS writer-disconnect handling, command timeouts, stale-SLAM
stopping, and firmware safety.

## Part 1: protocol validation

Implemented in `edge_agent/protocol.py`.

1. Parse the high-level command envelope.
2. Require a bounded command ID and an allowlisted action.
3. Reject expired commands, excessive lifetimes, malformed payloads, and
   non-finite numbers.
4. Pass only a validated `Command` to orchestration.
5. Retain the final result by command ID so a retry cannot repeat motion.

Before hardware use, add action-specific payload schemas and physical bounds.

## Part 2: workflow ownership

Implemented in `edge_agent/motion.py`.

1. Every behavior adapter registers for exactly one `MotionMode`.
2. `transition()` asks the current bbOS-backed behavior to stop and release its
   writer before starting the next behavior.
3. bbOS single-writer IPC remains the final ownership enforcement.
4. A duplicate command ID for the active mode is idempotent.
5. `stop_all()` bypasses the normal transition lock and invalidates an in-flight
   start using a transition epoch.
6. A stop/start timeout moves the coordinator to `faulted` instead of starting a
   competing owner.

Adapters must make `stop()` idempotent and acknowledge only after their bbOS
command writer is disabled or released. The coordinator must not implement a
second planner, base controller, or topic lock.

`BbosNavigationAdapter` writes one route to `nav.command`, keeps that writer
alive, observes `nav.state`, and completes only on `reached`. `failed` or an
unexpected return to `idle` fails the RealBot command. Camera `u/v` coordinates
must be projected into map-frame `x/y` before reaching this adapter.

## Part 3: controller watchdog

Implemented in `edge_agent/watchdog.py`.

1. The controlling browser sends a heartbeat every second.
2. The LiveKit session adapter accepts heartbeats only from the current authorized controller.
3. The robot records receipt using monotonic time.
4. Three seconds without a heartbeat invokes the RealBot workflow stop path.
5. An explicit participant-loss event stops sooner, but is not required for
   safety.
6. A new heartbeat clears lease expiry but never resumes the previous behavior.

This does not replace the deployed protections: `nav` stops on command-writer
disconnect or stale SLAM, `base` zeros stale/missing `drive.ctrl`, and the
firmware/ODrive layer has its own safety behavior. The three-second value is an
MVP session default and must be validated on the demo network.

## Part 4: bbOS-aligned capture evidence

Implemented in `edge_agent/capture.py`.

1. Use `camera.depth` as the reference reader and open RGB and SLAM pose readers
   with bbOS `aligned_to=depth_reader`, matching the deployed mapping daemon.
2. Let bbOS select buffered samples by source timestamp; do not maintain a
   parallel application-level synchronization buffer.
3. Reject aligned sets older than 750 ms, RGB/depth skew over 50 ms, or RGB/pose skew
   over 100 ms.
4. Assign a unique `capture_id` and retain map/calibration revisions.
5. Use the accepted bundle for pointing or camera-click geometry.

`AlignedCaptureBuilder` performs the final validation and packaging. It keeps
wall-clock bbOS timestamps separate from its monotonic evidence timestamp. It
does not yet calculate a pointing ray or retain captures for browser clicks.

## Part 5: orchestration

Implemented in `edge_agent/agent.py`.

1. Validate or deduplicate the incoming command.
2. Publish delivered, accepted, and executing states.
3. Acquire the corresponding motion mode.
4. Run persistent modes (`following`, `free_cam`) until an explicit stop,
   transition, or lease loss.
5. Await the adapter's authoritative terminal result for bounded modes
   (`navigating`, `interactable_test`) before returning to idle.
6. Publish a final result and cache it by command ID.
7. Include mode, owner, fault, lease, and last-stop reason in robot state.

The simulator supplies fake terminal results. The bbOS navigation adapter waits
for `nav.state=reached` and treats `failed` as failure; adapter `start()` alone
never means physical completion.

## Part 6: session lease

The behavior is implemented in `relay/app.py` for local simulation. The
hardware implementation belongs in the LiveKit session adapter after the
deployed participant identity and token contract are verified.

1. The first browser in a room receives the controller lease.
2. Other browsers remain view-only.
3. Only the controller's commands and heartbeats are forwarded.
4. Stop remains available to every connected viewer as a safety action.
5. Controller disconnect immediately sends `control_lost` to the robot.
6. The next browser is promoted, but no robot behavior resumes automatically.

Shared demo tokens distinguish robot, controller, and viewer connections.
Production identity, authorization, token rotation, and rate limits remain
separate work.

## Part 7: browser behavior

Implemented in `web/src/lib/remoteBotClient.ts` and `web/src/pages/ControlPage.tsx`.

1. Start heartbeat only after receiving the lease.
2. Stop heartbeat when the lease or socket is lost.
3. Disable ordinary controls for view-only clients.
4. Keep Stop available whenever the robot is online.
5. Display the authoritative motion mode and any coordinator fault.

## Part 8: interactables state machine

`edge_agent/interactables.py` now implements the realtor setup state machine and
the two independent verification gates. Gesture, speech, projection, and durable
storage remain adapters around that state machine rather than embedded in it.

1. Require localized SLAM before entering `ready`.
2. Convert a debounced open palm into exclusive following mode.
3. Stop following before accepting a synchronized 3D point.
4. Capture the spoken type and parameters, then read them back.
5. Require explicit confirmation before entering `interactable_test` mode.
6. Treat fault-free policy execution as the first verification gate.
7. Ask the realtor whether the physical result was correct as the second gate.
8. Save only after both gates pass; failure leaves a retryable draft.
9. Block setup exit until an unverified draft is cancelled or verified.
10. Narrate each meaningful transition through the injected speech callback.

## Part 9: hardware integration order

Do not connect all hardware adapters at once. Use this order:

1. Exercise `BbosNavigationAdapter` with a fake IPC backend, then verify
   `nav.command`/`nav.state` with wheels raised.
2. Prove normal arrival, route failure, stale-SLAM stop, command-writer release,
   browser lease expiry, and reconnect-without-resume.
3. Implement person following—the deployed bbOS and bbapps checkouts contain
   person detection but no person-follow controller or topic—and verify writer
   handoff while the base is secured.
4. Connect bbOS aligned RGB/depth/SLAM readers and record capture fixtures.
5. Connect Free Cam only after arm ownership, limits, collision checks, and its
   safe stop/park behavior are verified.
6. Connect one pretrained interaction action last, initially requiring realtor
   confirmation before saving a point.

Each adapter needs unit tests with fake IPC and a supervised on-robot test
showing start, normal completion, Stop, timeout, writer release, and process
shutdown. Do not import a whole daemon as a library; depend on its published
topic contract or extract a deliberately supported bbOS API.

## Run tests

```sh
cd edge_agent
uv sync
uv run pytest
```

The relay installs this package as an editable local dependency:

```sh
cd relay
uv sync
uv run pytest
```
