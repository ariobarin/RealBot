# MVP Edge Gap Patches

## Purpose

This document closes the five cross-cutting gaps that must be resolved before
the remote-control and interactables MVP is tested on moving hardware. It is
intentionally narrower than a production architecture.

The target vertical slice is:

```text
SLAM ready
  -> realtor starts setup
  -> open palm starts following
  -> pointing captures synchronized RGB + depth + pose
  -> realtor says "cabinet, opens left"
  -> robot reads it back and receives confirmation
  -> robot announces and performs the test
  -> realtor confirms whether it worked
  -> verified point appears on the map and camera feed
```

## Patch 1: one motion mode at a time

### Problem

Navigation, person following, Free Cam, and interactable testing can all command
physical resources. The MVP needs one place that decides which behavior owns the
base and arms.

### Minimal implementation

Add a `MotionCoordinator` inside the robot edge agent. It owns the current mode
and is the only component allowed to start a motion-producing behavior.

```python
class MotionMode(str, Enum):
    IDLE = "idle"
    NAVIGATING = "navigating"
    FOLLOWING = "following"
    FREE_CAM = "free_cam"
    INTERACTABLE_TEST = "interactable_test"


class MotionCoordinator:
    async def transition(self, target: MotionMode, command_id: str) -> None: ...
    async def stop_all(self, reason: str) -> None: ...
```

MVP ownership:

| Mode | Base | Left arm | Right arm |
| --- | --- | --- | --- |
| `idle` | stopped | parked | parked |
| `navigating` | navigation | unclaimed | unclaimed |
| `following` | follow controller | unclaimed | unclaimed |
| `free_cam` | locked | Free Cam | unclaimed |
| `interactable_test` | approach controller or locked | action policy | action policy if required |

Transition procedure:

1. Acquire the coordinator lock.
2. If the target mode already owns the resources for the same command, return
   its current status rather than starting it again.
3. Ask the current owner to stop.
4. Wait for a positive stopped/parked acknowledgement, with a two-second MVP
   timeout.
5. If acknowledgement does not arrive, enter `faulted`, send local Stop, reject
   the new behavior, and require realtor intervention.
6. Start the new owner and publish the new mode.

`stop` bypasses the normal command queue, invokes `stop_all`, and never waits for
a mode lock held by a slow operation. An action adapter cannot directly start a
base or arm writer; it must acquire the coordinator first.

Add these fields to authoritative robot state:

```json
{
  "motionMode": "following",
  "motionOwnerCommandId": "cmd_123",
  "motionSince": 1789812000000,
  "motionFault": null
}
```

### Acceptance checks

- Starting navigation while following first stops and acknowledges the follow
  controller.
- Free Cam and an interactable test cannot run together.
- A duplicate command does not reacquire a resource or repeat motion.
- Stop leaves the base stopped and both arms stopped or in their defined safe
  hold state regardless of the current mode.

## Patch 2: deterministic disconnect behavior

### Problem

The robot must not guess whether an absent browser still wants it to move.

### Minimal implementation

The edge agent maintains a control lease using a heartbeat once per second. The
MVP lease expires after three missed heartbeats. Use monotonic time locally;
never derive lease expiry from the browser clock.

On relay disconnect, controller disconnect, or lease expiry:

1. Immediately call `MotionCoordinator.stop_all("control_disconnected")`.
2. Stop navigation, person following, Free Cam, and any approach movement.
3. Ask an action policy to abort and move only through its already validated
   safe-abort behavior. Do not invent a generic retraction trajectory.
4. Mark a point being tested as `test_interrupted`; do not save it as verified.
5. Keep a captured draft locally so the realtor can explicitly retry or cancel
   after reconnecting.
6. Publish the failure reason when the relay becomes available again.

Reconnection returns an authoritative snapshot. Nothing resumes automatically;
the realtor must issue a new command with a new command ID. Old commands remain
expired and deduplicated.

The failsafe belongs on the robot. A relay-side disconnect notification is useful
but is not trusted as the only way to stop.

### Acceptance checks

- Killing the browser stops each motion mode within the configured watchdog
  interval.
- Killing the relay produces the same result.
- Reconnecting does not resume following, navigation, Free Cam, or testing.
- An interrupted test never creates a verified interactable.

## Patch 3: synchronized pointing capture

### Problem

A 2D finger or camera pixel is useful only when it is paired with the depth and
SLAM pose from the same moment. Using the newest independent samples can place a
point on the wrong object, especially while the robot or realtor is moving.

### Minimal implementation

Create a robot-side `CaptureBundle` before point geometry is calculated:

```python
@dataclass(frozen=True)
class CaptureBundle:
    capture_id: str
    captured_monotonic_ns: int
    rgb_timestamp_ns: int
    depth_timestamp_ns: int
    pose_timestamp_ns: int
    rgb: bytes
    depth_mm: NDArray[np.uint16]
    map_pose: Pose3D
    map_id: str
    map_revision: int
    calibration_revision: str
```

Maintain a bounded ring buffer of recent RGB, depth, and SLAM pose samples. For
each RGB frame, select the nearest depth and pose by source timestamp. Initial
MVP limits are:

- RGB-to-depth skew: at most 50 ms;
- RGB-to-pose skew: at most 100 ms;
- bundle age when the gesture is accepted: at most 750 ms.

These are starting limits and must be tuned from recorded robot data. Reject the
capture rather than extrapolating when a limit is exceeded.

For the MVP, stop the base before finalizing the point, collect a short stable
window, and compute the point from one accepted bundle. Save the `capture_id`,
timestamps, map ID/revision, and calibration revision with the draft.

The live-video envelope also carries `captureId` and `capturedAt`. A future
camera-click command references that exact ID:

```json
{
  "type": "move_to_view",
  "payload": { "captureId": "cap_123", "u": 0.41, "v": 0.62 }
}
```

If the ring buffer no longer contains that capture, the command is rejected as
`capture_expired`; it must never silently use the latest depth.

### Acceptance checks

- A known pixel and synchronized fixture reconstruct within the agreed spatial
  error bound.
- Excessive RGB/depth or RGB/pose skew produces a spoken retry request.
- Old capture IDs are rejected.
- Moving the robot between two frames cannot cause a click or pointing gesture
  from the first frame to use the second frame's pose.

## Patch 4: an explicit MVP test result

### Problem

The pretrained action can finish without proving that the real object moved as
intended. Automatic visual or force-based verification is too large for the
first slice, but saving the point without a pass signal is unsafe and misleading.

### Minimal implementation

Support one real interaction type first. Use `cabinet` only if the deployed robot
has a tested cabinet policy; otherwise select whichever existing policy is most
reliable during the Phase 0 robot audit.

A test has two gates:

1. **Robot execution gate:** the action adapter reports that planning/execution
   completed without a timeout, controller fault, safety stop, or policy error.
2. **Realtor observation gate:** the robot asks, "Did the cabinet open correctly?"
   and receives an explicit realtor `yes` within 30 seconds.

Only both gates produce `verified`. `No`, timeout, ambiguous speech, disconnect,
or execution failure produces `failed` or `test_interrupted` and does not save a
visitor-visible point.

Record the evidence:

```json
{
  "verification": {
    "method": "realtor_confirmation",
    "executionResult": "succeeded",
    "confirmed": true,
    "confirmedBy": "active_realtor_session",
    "confirmedAt": "RFC3339 timestamp",
    "policyVersion": "cabinet-v1"
  }
}
```

Spoken `yes`/`no` is the primary interface. The authenticated realtor UI may
show equivalent Yes/No buttons as an accessibility and noisy-room fallback; the
same backend intent and authorization checks handle both.

Required narration:

```text
"I'm testing the cabinet now."
"The motion completed. Did the cabinet open correctly?"
"The test passed. I saved this cabinet."
```

or:

```text
"The test did not pass. I did not save this cabinet."
```

### Acceptance checks

- An action-controller success without realtor confirmation does not save.
- Realtor confirmation after an action failure does not save.
- Confirmation from a visitor or expired session is rejected.
- A verified point includes the policy version and confirmation audit fields.

## Patch 5: minimum demo security

### Problem

A public relay that accepts a predictable room name is not sufficient when it
can command moving hardware. The MVP does not need accounts, organizations, or a
production identity service, but it does need unguessable credentials and strict
authorization.

### Minimal implementation

- Generate at least 128 bits of randomness for each demo session credential.
- Use separate credentials for the robot and realtor. A visitor token is
  view-only and cannot be promoted by changing a client-side role.
- Store robot credentials in environment variables or an ignored local secret
  file, never in Git or a frontend bundle.
- Use `wss://` outside localhost and verify the server certificate.
- Authenticate before joining a relay room. Do not put secrets in URLs or logs.
- Allow one active realtor control lease per robot. A second realtor may view but
  cannot command until the lease is released or expires.
- Accept only the documented high-level command allowlist and validate every
  payload, finite number, coordinate bound, action name, command ID, session ID,
  and expiry on the robot.
- Rate-limit ordinary commands per session. Keep Stop on a separate priority path
  and do not reject it because the ordinary-command bucket is full.
- Bound JSON, JPEG, and compressed-map message sizes before allocation or
  decompression.

For the hackathon, credentials may be generated when the relay starts and shared
out of band with the operator. They expire when the demo session ends. This is a
deliberate temporary mechanism, not the future customer authentication system.

### Acceptance checks

- A client with only a visitor token cannot issue any command.
- An incorrect, expired, or missing credential cannot join the control channel.
- A second realtor cannot steal an active lease.
- Unknown actions, malformed payloads, expired commands, and non-finite
  coordinates are rejected by the robot even if the relay forwards them.
- Flooding video or ordinary commands does not delay Stop.

## Implementation order

Implement the patches in this order:

1. `MotionCoordinator` and global Stop.
2. Heartbeat lease and disconnect failsafe.
3. Capture bundles and timestamp validation.
4. One action's two-gate verification flow.
5. Demo credentials, controller lease, and limits.

The simulator should implement the same contracts first. The browser can then
exercise mode conflicts, disconnects, stale captures, failed confirmation, and
unauthorized commands without moving hardware.

## MVP completion gate

The gap patches are complete when one automated integration scenario proves:

1. The authenticated realtor obtains the single control lease.
2. SLAM is ready and the robot enters following mode.
3. Pointing stops following and creates a time-synchronized candidate.
4. The label and direction are read back and explicitly confirmed.
5. The coordinator enters interactable-test mode and no other motion mode runs.
6. Robot execution and realtor observation both pass.
7. The verified point is saved and rendered using its real map coordinates.
8. Repeating the scenario with a disconnect, stale frame, failed action, `no`,
   or unauthorized client leaves no verified point and stops motion safely.

## Deferred gap register

The five patches above are sufficient for a supervised MVP. The gaps below are
deliberately not included in that scope. Their criticalness describes when they
become mandatory:

- **Critical:** required before unsupervised operation or public access to real
  hardware;
- **High:** required before a customer/property pilot;
- **Medium:** required for reliable multi-robot operation or product quality;
- **Low:** valuable improvement that can wait without invalidating the core
  architecture.

| Deferred gap | Criticalness | Required before | What must eventually be added |
| --- | --- | --- | --- |
| Independent physical safety layer | **Critical** | Unsupervised robot motion | Hardware E-stop integration, person/pet proximity stops, contact/force limits, safe-speed zones, fault-latched recovery, and validation that safety does not depend on the browser, relay, speech model, or main edge process |
| Production identity and authorization | **Critical** | Public internet access beyond a controlled demo | Provisioned per-device credentials, user accounts, robot ownership, role-based permissions, credential rotation/revocation, audit records, and recovery for a compromised device |
| Signed updates and rollback | **Critical** | Remotely updating deployed robots | Signed artifacts, pinned dependencies, atomic install, preflight checks, health-based rollback, configuration migration, and a known-good recovery image |
| Automatic interaction success verification | **High** | Customer pilot without a supervising realtor | Type-specific visual, joint, force, or state verification so success does not depend solely on a person's spoken Yes/No |
| Calibration lifecycle | **High** | Repeated use after repair or sensor movement | Versioned camera intrinsics/extrinsics, calibration health checks, expiry/recalibration workflow, and invalidation of points captured with incompatible calibration |
| Complete map lifecycle | **High** | Saving multiple real properties | Authoritative map identity, save/load, revision versus replacement, deletion, wrong-map protection, interaction migration, loop-closure handling, and stale-point revalidation |
| Privacy and data retention | **High** | Recording inside an occupied property | Recording indication and consent, rules for raw audio/video and snapshots, face/person handling, encryption at rest, retention periods, export/deletion, and cloud-processing disclosure |
| Operational observability | **High** | Customer pilot or remote support | Correlated logs and metrics for command, session, map, capture, mode, safety decision, action result, resource use, clock skew, and software/model/configuration versions |
| Compute, thermal, and bandwidth budgets | **High** | Running SLAM, vision, speech, and actions together for long sessions | Measured CPU/GPU/memory/temperature/network ceilings, priority scheduling, queue limits, load shedding, and proof that Stop/control remain responsive under overload |
| Protocol and capability negotiation | **High** | More than one robot/software version | Versioned handshake, advertised sensors/actions/features, backward-compatibility policy, unsupported-command behavior, schema fixtures, and staged rollout checks |
| Durable state and restart recovery | **High** | Leaving verified points on a deployed robot | Transactional interactable storage, corruption handling, backups, migrations, restart reconciliation, and durable records for safety-significant action outcomes |
| Threat hardening | **High** | Customer pilot on untrusted networks | Security review, dependency scanning, secret isolation, replay protection across restarts, decompression-bomb defenses, denial-of-service tests, relay abuse controls, and incident credential revocation |
| Speech robustness and privacy-preserving fallback | **Medium** | Noisy or multilingual properties | Wake/turn-taking rules, confidence thresholds, correction UI, supported languages, local-versus-cloud speech behavior, and explicit behavior when speech service is unavailable |
| Multi-viewer and controller policy | **Medium** | Multiple staff or visitors joining one robot | Viewer limits, controller handoff, lease queueing, moderator controls, presence indicators, and consistent authorization across reconnects |
| Production media transport | **Medium** | Scaling beyond the JPEG demo stream | WebRTC, STUN/TURN, adaptive bitrate, congestion handling, camera permission policy, multi-viewer distribution, and end-to-end media metrics |
| Fleet provisioning and device recovery | **Medium** | Operating multiple customer robots | Manufacturing identity, claim/unclaim, enrollment, inventory, configuration rollout, lost-device handling, factory reset, and support access controls |
| Map and interaction backup/export | **Medium** | Customers depending on saved setups | Encrypted backup, restore testing, property export/import, ownership checks, and safe behavior when a restore references missing policies or calibration |
| Long-duration reliability testing | **Medium** | Pilot use measured in hours or days | Soak tests, repeated reconnects, memory-leak checks, disk exhaustion, clock changes, thermal cycles, sensor restarts, and recovery from individual process crashes |
| Accessibility and richer operator guidance | **Low** | Broader user rollout | Captions, non-speech confirmation, color-independent point states, clearer recovery coaching, adjustable narration, and accessible controls |
| Analytics and setup optimization | **Low** | Product refinement | Funnel metrics, setup-time analysis, failure clustering, action success trends, and privacy-reviewed diagnostics for improving gesture and interaction flows |

### Promotion gates

Use the register as release gates rather than pulling every row into the MVP:

1. **Supervised MVP:** complete only the five patches in this document; keep an
   operator beside the robot with the hardware E-stop available.
2. **Public remote demo:** additionally close the Critical production-identity
   exposure appropriate to the demo and prove the physical safety layer for the
   demonstrated behaviors.
3. **Property pilot:** close every Critical and High row, or explicitly disable
   the capability that depends on an open row.
4. **Multi-robot product:** close the relevant Medium rows and establish measured
   reliability targets before scaling the fleet.
