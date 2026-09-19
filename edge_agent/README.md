# RealBot edge agent foundation

This package contains robot-authoritative runtime logic. It is intentionally
independent from FastAPI and browser code so the real robot and
`relay/simulator.py` can use the same safety and protocol behavior.

No module in this package connects to hardware yet. Hardware integration must
be added through adapters only after the deployed bbOS interfaces are audited.

## Responsibility split

```text
web:        sends heartbeat and high-level commands; renders authoritative state
relay:      grants one controller lease and forwards messages
edge agent: validates commands, owns motion modes, expires the lease, stops locally
adapters:   translate an owned mode into audited bbOS navigation/arm/follow calls
```

The relay is not a safety boundary. If the relay or browser disappears, the
robot-local watchdog stops all active modes.

## Part 1: protocol validation

Implemented in `edge_agent/protocol.py`.

1. Parse the high-level command envelope.
2. Require a bounded command ID and an allowlisted action.
3. Reject expired commands, excessive lifetimes, malformed payloads, and
   non-finite numbers.
4. Pass only a validated `Command` to orchestration.
5. Retain the final result by command ID so a retry cannot repeat motion.

Before hardware use, add action-specific payload schemas and physical bounds.

## Part 2: motion ownership

Implemented in `edge_agent/motion.py`.

1. Every motion adapter registers for exactly one `MotionMode`.
2. `transition()` stops and acknowledges the current owner.
3. Only after the old owner stops does the new adapter start.
4. A duplicate command ID for the active mode is idempotent.
5. `stop_all()` bypasses the normal transition lock and invalidates an in-flight
   start using a transition epoch.
6. A stop/start timeout moves the coordinator to `faulted` instead of starting a
   competing owner.

Hardware adapters must make `stop()` idempotent and must acknowledge only after
their bbOS writer is no longer producing movement.

## Part 3: controller watchdog

Implemented in `edge_agent/watchdog.py`.

1. The controlling browser sends a heartbeat every second.
2. The relay forwards heartbeats only from the current lease holder.
3. The robot records receipt using monotonic time.
4. Three seconds without a heartbeat invokes robot-local `stop_all()`.
5. An explicit relay `control_lost` message stops sooner, but is not required for
   safety.
6. A new heartbeat clears lease expiry but never resumes the previous behavior.

The three-second value is an MVP default and must be validated on the demo
network before moving hardware.

## Part 4: synchronized capture

Implemented in `edge_agent/capture.py`.

1. Feed RGB, depth, and SLAM pose samples into bounded timestamped queues.
2. Select depth and pose nearest to the chosen RGB timestamp.
3. Reject bundles older than 750 ms, RGB/depth skew over 50 ms, or RGB/pose skew
   over 100 ms.
4. Assign a unique `capture_id` and retain map/calibration revisions.
5. Use the accepted bundle for pointing or camera-click geometry.

The current code defines and tests synchronization only. It does not yet connect
to bbOS topics, calculate a pointing ray, or retain a capture ring for browser
clicks.

## Part 5: orchestration

Implemented in `edge_agent/agent.py`.

1. Validate or deduplicate the incoming command.
2. Publish delivered, accepted, and executing states.
3. Acquire the corresponding motion mode.
4. Run persistent modes (`following`, `free_cam`) until an explicit stop,
   transition, or lease loss.
5. Complete bounded modes (`navigating`, `interactable_test`) and return to idle.
6. Publish a final result and cache it by command ID.
7. Include mode, owner, fault, lease, and last-stop reason in robot state.

Navigation completion is simulated. The hardware adapter must wait for the local
navigation state machine and arrival tolerance before returning.

## Part 6: relay lease

Implemented in `relay/app.py`.

1. The first browser in a room receives the controller lease.
2. Other browsers remain view-only.
3. Only the controller's commands and heartbeats are forwarded.
4. Stop remains available to every connected viewer as a safety action.
5. Controller disconnect immediately sends `control_lost` to the robot.
6. The next browser is promoted, but no robot behavior resumes automatically.

This is a demo lease, not authentication. Tokens, roles, rate limits, and
production identity remain separate work.

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

1. Implement a local Stop adapter and verify it with wheels raised.
2. Connect navigation state/commands and prove timeout plus disconnect Stop.
3. Locate and wrap the deployed follow controller; verify follow-to-navigation
   handoff while the base is secured.
4. Connect RGB/depth/SLAM timestamp readers and record synchronization fixtures.
5. Connect Free Cam only after arm ownership, limits, collision checks, and its
   safe stop/park behavior are verified.
6. Connect one pretrained interaction action last, initially requiring realtor
   confirmation before saving a point.

Each adapter needs unit tests with a fake bbOS client and a supervised on-robot
test showing start, normal completion, Stop, timeout, and process shutdown.

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
