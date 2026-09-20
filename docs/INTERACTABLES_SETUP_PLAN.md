# Interactables Setup Plan

> **Priority update (2026-09-19):** This workflow is secondary to the
> [visitor camera and click-to-drive MVP](VISITOR_DRIVING_MVP.md). Preserve these
> requirements for later implementation; they do not gate the current MVP.

## 1. Product outcome

After a realtor completes a SLAM scan, they can teach the robot where the
space's interactable objects are. The robot follows the realtor, detects an
open-palm gesture to begin the capture flow, resolves the realtor's pointing
gesture to a 3D point, listens for an interaction type, confirms it aloud,
performs the pretrained action as a first test, and saves the interactable only
when that test succeeds.

During setup, the realtor sees the live camera, the SLAM map, the robot's pose,
and every candidate or verified interaction point. During a later visitor
session, only verified interactables may be invoked.

This plan makes one interpretation explicit: "the robot says every action" means
that it announces every meaningful state transition, command, and physical
action. It does not narrate individual sensor frames or controller updates.

The concrete MVP contracts for motion ownership, disconnects, synchronized
point capture, realtor-confirmed testing, and demo security are specified in
[`MVP_EDGE_GAP_PATCHES.md`](MVP_EDGE_GAP_PATCHES.md).

## 2. Fixed product rules

- Setup is realtor-only and is available only after a SLAM scan is complete and
  the robot is localized in that map.
- A bare, open hand facing the robot starts the tracking/following workflow.
- The robot uses an existing follow controller if one is available on the deployed
  robot. The exact API or bbOS topic still needs to be located and wrapped.
- A bare pointing hand selects a 3D surface point. Each interaction type defines
  which physical component the realtor should point at, such as a cabinet handle
  rather than the middle of the cabinet door.
- The realtor speaks the type. The robot reads back the type and location and
  requires an explicit spoken confirmation before moving to test it.
- Operation is pretrained by interaction type; setup labels and parameterizes an
  interaction but does not demonstrate a new manipulation policy.
- The robot announces intent before every movement or physical action and
  announces success, failure, or cancellation afterward.
- The first physical test must pass before an interaction is committed as
  `verified`.
- The realtor may end setup at any safe boundary. Setup cannot end while a new
  point is approaching or testing; the current attempt must pass, fail, or be
  cancelled into a safe state first.
- Nearby interaction points are valid and remain distinct.
- The realtor view shows points on the SLAM map and as projected dots in the live
  camera feed.
- Only the realtor can create, edit, retest, disable, or delete points. Visitors
  may invoke only allowlisted, verified points.

## 3. What the repository already provides

The implementation should adapt existing robot capabilities rather than replace
them:

- `nav/main.py` and bbOS provide SLAM, map, localization, route planning, and
  base motion. The observed telemetry contract is documented in
  [`SLAM_TELEMETRY.md`](SLAM_TELEMETRY.md).
- `greeter/main.py` has camera-frame ingestion and YOLO-based person detection.
  It is a useful person-observation source, not yet a follow controller.
- `greeter/main.py` also demonstrates microphone input, speaker output, and a
  Gemini Live voice session. Those integrations should be extracted behind a
  small speech adapter rather than importing the whole greeter application.
- `quest_teleop/scripts/tracking.py` contains coordinate transforms, pose-glitch
  rejection, inverse-kinematics targets, and speed limits. Its input is a Quest
  controller, so it does not satisfy bare-hand detection, but its safety and
  transform patterns are reusable.
- `inference/` and the current action command path provide a starting point for
  calling pretrained behaviors.

Not yet found in this checkout:

- the deployed robot's prebuilt person-follow API or topic;
- a bare-hand landmark, open-palm, or pointing detector;
- a typed registry of pretrained interaction behaviors;
- an interactable setup state machine and durable store;
- camera projection of saved map points.

Finding and validating those interfaces is Phase 0, not an assumption hidden in
the implementation.

## 4. Robot-side architecture

The robot is authoritative for setup state, safety, map coordinates, and test
results. A new `interactables` orchestrator should coordinate narrow adapters:

```text
camera + depth ----> HandPoseAdapter ----> pointing candidate
SLAM + transforms -> SlamAdapter --------> map-relative 3D point
person detector ---> FollowAdapter ------> safe follow/stop
microphone --------> SpeechAdapter ------> label/confirm/correct
speaker -----------> Narrator ----------> spoken intent/outcome
action policies ---> ActionRegistry -----> validate/test/invoke
                                            |
                                            v
                         InteractableSetupOrchestrator
                            |              |
                            v              v
                    InteractableStore   TelemetryPublisher
                                             |
                                             v
                                      edge relay + web app
```

The orchestrator owns the state machine. Adapters must not independently start
base or arm movement. This gives stop, SLAM-loss, authorization, timeouts, and
voice announcements one enforcement point.

Suggested package:

```text
interactables/
|-- main.py
|-- orchestrator.py
|-- state.py
|-- models.py
|-- store.py
|-- action_registry.py
|-- narration.py
|-- geometry.py
|-- adapters/
|   |-- slam.py
|   |-- follow.py
|   |-- hand_pose.py
|   |-- speech.py
|   `-- actions.py
`-- tests/
```

## 5. Setup state machine

```text
UNAVAILABLE --scan complete/localized--> READY
READY --open palm--> FOLLOWING
FOLLOWING --valid point gesture--> CAPTURING_POINT
CAPTURING_POINT --stable 3D point--> AWAITING_LABEL
AWAITING_LABEL --recognized type--> AWAITING_CONFIRMATION
AWAITING_CONFIRMATION --yes--> APPROACHING
APPROACHING --safe/reachable--> TESTING
TESTING --pass--> VERIFIED --> FOLLOWING
TESTING --fail--> RETRY_REQUIRED
RETRY_REQUIRED --retry/move/cancel--> TESTING/CAPTURING_POINT/FOLLOWING

any safe state --end setup--> ENDING --> READY
any active state --SLAM lost--> PAUSED_SLAM
any active state --stop/E-stop--> safe stopped state
```

State behavior:

1. `READY`: announce that setup is available and wait for the realtor.
2. `FOLLOWING`: announce "I'll follow you" once, engage the follow adapter, and
   maintain a configurable distance. Do not repeat narration on every follow
   controller update.
3. `CAPTURING_POINT`: stop the base, announce the detected gesture, gather a
   short window of hand/depth observations, and reject an unstable point.
4. `AWAITING_LABEL`: show an unsaved candidate dot and ask for the type.
5. `AWAITING_CONFIRMATION`: read back the parsed type and position, including
   required type parameters; proceed only after an explicit "yes".
6. `APPROACHING`: announce the approach, reserve base/arm control, and move to a
   validated approach pose.
7. `TESTING`: announce the exact test, execute it, and observe the policy result.
8. `VERIFIED`: announce success, commit atomically, publish the point, and resume
   following if the realtor remains in setup.
9. `RETRY_REQUIRED`: announce the failure and accept `try again`, `move point`,
   `cancel`, or `delete <name>` as appropriate.
10. `PAUSED_SLAM`: stop all motion, announce localization loss, retain the draft,
    and resume only after the same map is recovered and the candidate revalidates.

An open-palm or point gesture should be stable for a short debounce window (start
with 500 ms) and meet a confidence threshold. Both values remain tunable from
recorded data.

## 6. Turning a pointing gesture into a map point

The initial implementation should use the head RGB/depth stream and a bare-hand
landmark model:

1. Detect one hand and classify open palm versus pointing.
2. Form a ray along the index finger, for example from the index knuckle through
   the fingertip.
3. Intersect the ray with fresh depth data. Use a small temporal and spatial
   median rather than a single depth pixel.
4. Estimate a local surface normal from neighboring depth samples.
5. Transform the point and normal from camera coordinates through the calibrated
   camera-to-base transform into the active SLAM map frame at the capture time.
6. Keep samples only when their map-space variance is below a threshold.
7. Validate range, occlusion, surface quality, map freshness, and approach/reach
   feasibility before asking for confirmation.

The candidate must carry the exact image timestamp, camera calibration revision,
robot pose, map ID, and map revision used for the transform. If depth is stale,
SLAM is not localized, more than one hand is ambiguous, or the candidate cannot
be approached safely, the robot explains the problem and asks the realtor to
point again.

### Type-specific pointing rules

| Type | Realtor points at | Required parameters | Initial test |
| --- | --- | --- | --- |
| `cabinet` | Handle or grasp point | opens `left` or `right` | Open slightly, verify motion, return safely |
| `drawer` | Center of handle | pull direction, inferred from surface normal when reliable | Pull slightly, verify motion, return safely |
| `light_switch` | Switch face | desired state `on` or `off`; switch style if needed | Set requested state and verify if observable |
| `fridge` | Handle or grasp point | opens `left` or `right` | Open slightly, verify motion, return safely |

The registry owns these rules so new types can be added without changing the
state machine. If a required parameter cannot be inferred, the robot asks for it
verbally and reads it back during confirmation.

## 7. Voice and narration contract

The speech adapter produces constrained intents, not arbitrary commands:

- labels and parameters: `cabinet`, `drawer`, `light switch`, `fridge`, `left`,
  `right`, `on`, `off`;
- confirmation: `yes`, `no`;
- correction: `try again`, `move point`, `cancel`, `delete <name>`;
- session control: `end setup`, `stop`.

The narrator speaks before motion and after completion. Example sequence:

1. "Open hand detected. I'll follow you."
2. "Pointing detected. I'm stopping to capture the location."
3. "I found a point on the cabinet handle. What type is it?"
4. "I heard cabinet, opening to the left, at this point. Should I test it?"
5. "I'm moving into position to test the cabinet."
6. "I'm opening the cabinet slightly now."
7. "The test passed. I saved this cabinet."

Failures are equally explicit: "I lost localization, so I stopped," "I couldn't
get reliable depth at that point," or "The cabinet test failed and I did not save
it." A spoken `stop` must preempt recognition playback and physical action.

The web app should receive the same structured narration events so it can show a
transcript without deriving robot state from speech text.

## 8. Interactable record

Raw `x, y, z` is necessary but not sufficient. A durable record should contain:

```json
{
  "id": "int_01...",
  "name": "kitchen upper cabinet",
  "type": "cabinet",
  "schemaVersion": 1,
  "map": {
    "id": "space_01",
    "revision": 12,
    "frame": "map"
  },
  "point": { "x": 1.42, "y": -0.88, "z": 1.06 },
  "surfaceNormal": { "x": 0.02, "y": -0.99, "z": 0.08 },
  "interactionPose": { "position": {}, "orientation": {} },
  "approachPose": { "position": {}, "orientation": {} },
  "parameters": { "opens": "left" },
  "evidence": {
    "capturedAt": "RFC3339 timestamp",
    "confidence": 0.94,
    "cameraPose": {},
    "snapshotRef": "local asset reference",
    "cropRef": "local asset reference",
    "visualDescriptorRef": "local descriptor reference"
  },
  "status": "verified",
  "test": {
    "policyVersion": "cabinet-v1",
    "passedAt": "RFC3339 timestamp",
    "metrics": {}
  },
  "createdBy": "realtor identity",
  "createdAt": "RFC3339 timestamp",
  "updatedAt": "RFC3339 timestamp"
}
```

For the first version, use local SQLite or an atomically replaced JSON file on
the robot. Only `verified` records are visitor-visible. Draft and failed records
are recoverable for the realtor but never visitor-invokable.

SLAM loop closure can move map coordinates. Save the capture pose/keyframe and
visual evidence in addition to coordinates. If bbOS publishes a map-revision
transform, apply it. If not, compare the stored crop/descriptor after relocalizing
and mark the point `stale` until revalidated when confidence is insufficient.

## 9. Safety and ownership

- Require an authenticated realtor role and an exclusive setup lease.
- Permit setup only with healthy SLAM, fresh transforms, and required sensors.
- Stop following before point capture, approach planning, or arm execution.
- Never run the follow controller and a manipulation test concurrently.
- Announce movement before commanding it.
- Keep `stop` and hardware E-stop higher priority than speech, setup, navigation,
  or action execution.
- Apply approach clearance, reachability, joint, velocity, force, and timeout
  limits before and during each test.
- On SLAM loss, stale depth, person loss, relay loss, or expired lease, stop safely
  and publish the reason.
- A failed or interrupted first test never creates a verified interaction.
- All commands are idempotent and carry a unique command ID, actor, expiry, and
  current map ID.

## 10. Edge and web protocol

Suggested high-level commands:

```text
setup_start
setup_end
setup_cancel_current
setup_retry_current
setup_move_current_point
interactable_update
interactable_delete
interactable_retest
use_interactable
stop
```

Suggested events/snapshots:

```text
setup_state
gesture_state
point_candidate
speech_intent
narration
test_status
interactable_saved
interactable_updated
interactable_deleted
interactable_snapshot
```

Every message includes protocol version, robot ID, map ID/revision, timestamp,
monotonic sequence number, and correlation/command ID where applicable. The web
client renders authoritative state; it does not advance setup optimistically.

## 11. Realtor web experience

After the SLAM scan reaches a ready state, enable **Start interactables setup**.
The setup view should include:

- live camera with projected interaction dots;
- SLAM map with robot pose and candidate/verified/stale points;
- current setup state and structured narration transcript;
- the pending label, type parameters, confidence, and test result;
- persistent `Stop`, plus context-sensitive `Cancel`, `Try again`, `Move point`,
  `Delete`, and `End setup` controls.

Dot states should be visually distinct: draft, awaiting confirmation, testing,
verified, failed, and stale. Project a 3D map point into the camera only when the
current map transform and camera calibration are fresh. Hide or edge-pin points
behind the camera or outside the frame; do not place an invented 2D dot. Nearby
points should be individually selectable and labeled on hover/tap.

The visitor experience receives only verified points and the allowlisted action
for each point. It cannot edit geometry, type, or policy parameters.

## 12. Failure and recovery behavior

| Condition | Robot behavior | Recovery |
| --- | --- | --- |
| False or brief gesture | Ignore; no narration until debounce passes | Hold gesture steadily |
| Ambiguous/multiple hands | Stop capture and explain ambiguity | Point again with one visible hand |
| Missing/stale depth | Do not create candidate | Reposition and point again |
| SLAM/localization lost | Stop base/arms and announce pause | Resume after same map relocalizes |
| Label uncertain | Read possible label without committing | Realtor repeats or cancels |
| Required parameter missing | Ask a constrained question | Realtor says left/right/on/off |
| Point unreachable | Do not approach | Move point or cancel |
| Test fails/times out | Return to safe pose; do not save | Retry, move point, or cancel |
| Relay/UI disconnects | Robot remains safe and authoritative | Reconnect to current snapshot |
| Map revision invalidates point | Mark stale and block visitor use | Revalidate with realtor |

## 13. Implementation phases

### Phase 0: deployed-interface audit

- Locate and exercise the prebuilt follow controller on the actual robot.
- Record its start, target, status, stop, timeout, and failure interfaces.
- Inventory action policies and define the first real type contract.
- Confirm synchronized RGB/depth timestamps and all camera/base/map transforms.
- Capture short, consented test recordings for gesture/depth threshold tuning.

Deliverable: adapter contracts and a hardware capability matrix. Do not begin
autonomous physical tests until this is complete.

### Phase 1: orchestrator with simulated adapters

- Implement the state machine, command IDs, safe cancellation, narration events,
  and an in-memory store.
- Simulate gestures, labels, navigation, and action results.
- Make the web view render setup state, map points, camera dots, and transcript
  from the protocol rather than hard-coded random data.

Deliverable: the full workflow can be demonstrated safely with deterministic
fake telemetry that uses the real schemas.

### Phase 2: gestures, geometry, and speech

- Integrate the bare-hand landmark model and gesture debounce.
- Implement depth-ray intersection, surface normal, temporal filtering, and map
  transforms with recorded-data tests.
- Extract speech input/output into the constrained adapter and implement type,
  parameter, confirmation, correction, and stop intents.

Deliverable: a realtor can create a stable, confirmed candidate without robot
manipulation.

### Phase 3: following, storage, and overlays

- Connect the audited follow controller with hard stop/ownership boundaries.
- Add durable local storage, map revision handling, and stale-point behavior.
- Validate camera-dot projection and nearby-point selection against known targets.

Deliverable: the robot follows, captures, persists, and displays points.

### Phase 4: one physical action end to end

- Start with one action whose policy and success signal are best supported by the
  deployed robot; use `cabinet` if its pretrained policy is ready.
- Validate approach planning, narration ordering, safety limits, pass/fail
  observation, atomic commit, retry, and cancellation.
- Add the remaining registry types only after the first vertical slice is stable.

Deliverable: one real interaction is verified only after a successful first test.

### Phase 5: visitor use and reliability

- Expose verified interactions to visitors through `use_interactable`.
- Add map-loop-closure recovery, retest/version migration, audit history, lease
  expiry, reconnect tests, and prolonged on-robot soak tests.

Deliverable: verified points survive normal restarts and map updates and are safe
to invoke from the visitor experience.

## 14. Test strategy

- Unit-test every state/event pair, especially stop, cancel, SLAM loss, duplicate
  command, and the rule that failed tests cannot be saved.
- Test pointing geometry with calibrated synthetic fixtures and recorded RGB/depth
  frames with known targets.
- Test speech with paraphrases, noise, misrecognitions, and interruption by stop.
- Contract-test each action type's required fields and policy version.
- Run relay/web integration tests with deterministic simulated robot telemetry.
- Run hardware tests in increasing risk order: wheels/arms disabled, base only,
  arm free-space rehearsal, then supervised object contact with E-stop ready.
- Log timestamps for gesture, pose, depth, speech, narration, commands, and results
  so failures can be reproduced rather than guessed from UI video.

## 15. Definition of done

The first production-shaped slice is complete when:

1. Setup cannot start without a completed map and healthy localization.
2. A realtor can trigger follow with a stable open palm and stop it immediately.
3. A pointing gesture produces a repeatable 3D map point with visible confidence.
4. The robot accepts a spoken type/parameters and obtains explicit confirmation.
5. Every meaningful transition and physical action is announced in the correct
   order and mirrored as structured telemetry.
6. The robot approaches and tests one pretrained type within enforced limits.
7. A point is saved only after a successful first test.
8. The saved point appears correctly on both the SLAM map and camera feed.
9. SLAM loss, failure, cancellation, and disconnect all stop safely and recover
   without creating a false verified point.
10. Only the realtor can mutate points, and only verified points can be invoked by
    a visitor.

## 16. Decisions to close during implementation

These do not block the architecture, but Phase 0 or recorded-data testing must
choose them:

- exact deployed follow interface and person-target handoff;
- hand landmark model, confidence threshold, and debounce duration;
- authoritative success signal for each pretrained action;
- storage choice between SQLite and atomic JSON;
- whether bbOS exposes a map-revision transform after loop closure;
- final correction vocabulary and whether the UI may confirm/correct in parallel
  with speech;
- naming rules when multiple interactions have the same type in one area.
