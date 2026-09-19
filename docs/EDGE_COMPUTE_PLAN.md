# RealBot Edge Compute Plan

> **Implementation status (2026-09-19): simulator-backed foundation, not a
> deployed robot runtime.** A read-only audit of `bracketbot-0187` confirmed
> that bbOS already owns navigation, SLAM health, timestamp alignment,
> single-writer IPC, stale-command stopping, motor safety, and a LiveKit
> `remote_session` daemon. RealBot should add thin topic adapters and product
> orchestration rather than recreate those systems. See
> [`../edge_agent/README.md`](../edge_agent/README.md) for the audited boundary.
> LiveKit through bbOS `remote_session` is the selected hardware transport; see
> [`LIVEKIT_ARCHITECTURE.md`](LIVEKIT_ARCHITECTURE.md). Older relay/JPEG sections
> in this plan describe the existing simulator and are not deployment guidance.

## 1. Purpose

Build a Python edge agent that runs on the robot alongside bbOS. The agent adds
RealBot workflow behavior at the verified extension point of the existing
LiveKit `remote_session` and bridges high-level intent to local bbOS services.

The edge agent does not replace navigation, SLAM, collision avoidance, or motor control. Those remain local responsibilities of `nav/main.py` and bbOS. The edge agent accepts high-level remote intent, validates it, forwards it to the correct local subsystem, and publishes authoritative robot telemetry.

The post-SLAM realtor teaching workflow is specified separately in
[`INTERACTABLES_SETUP_PLAN.md`](INTERACTABLES_SETUP_PLAN.md). The edge agent
transports its high-level commands and authoritative state, while its robot-side
orchestrator owns gesture, speech, safety, testing, and persistence.

The minimal cross-cutting contracts required before moving-hardware MVP testing
are defined in [`MVP_EDGE_GAP_PATCHES.md`](MVP_EDGE_GAP_PATCHES.md): exclusive
motion ownership, disconnect failsafe, synchronized capture, test verification,
and demo security.

```text
Browser
   | HTTPS token/persistence + LiveKit media/data
   v
bbOS remote_session + RealBot workflow bridge
   |-- nav.command/nav.state -> existing planner and base controller
   |-- aligned bbOS readers  -> camera, depth, pose, SLAM health
   `-- approved action adapters -> existing arm/policy applications
```

## 2. Hackathon scope

The first working vertical slice must support:

1. A browser and robot on different networks joining one authorized LiveKit room.
2. Robot pose and status telemetry.
3. Authoritative navigation goal, route, and planning status telemetry.
4. Existing robot camera tracks delivered through LiveKit.
5. `move_to`, `stop`, and one approved `use_action` behavior.
6. `move_to_view` after camera/depth projection is validated.
7. Command expiry and duplicate-command protection.
8. Reconnection without replaying expired commands.
9. A visitor minimap using the robot's authoritative pose and route.
10. A Free Cam mode that locks the mobile base, safely moves the left arm, and streams its camera
    until the visitor exits the mode.

The robot's existing LiveKit/WebRTC remote-session implementation is the chosen
transport. The disposable WebSocket relay remains a simulator only and must not
be deployed as a competing hardware or camera owner. Durable interactable
storage is separate application-backend work; a general-purpose remote shell
remains out of scope.

The separately planned browser authentication work is documented in
[`REALTOR_GUEST_AUTH_PLAN.md`](REALTOR_GUEST_AUTH_PLAN.md) and does not add robot
authentication responsibilities to the edge agent.

## 3. Proposed package

```text
edge_agent/
|-- __init__.py
|-- main.py
|-- agent.py
|-- config.py
|-- protocol.py
|-- relay_client.py
|-- adapters/
|   `-- bbos_navigation.py
|-- navigation_telemetry.py
|-- camera.py
|-- projection.py
|-- free_cam.py
|-- actions.py
|-- command_journal.py
`-- tests/
    |-- test_protocol.py
    |-- test_command_journal.py
    |-- test_bbos_navigation.py
    |-- test_navigation_telemetry.py
    |-- test_projection.py
    `-- test_agent_integration.py
```

## 4. Runtime data flow

### 4.1 Commands

```text
Browser command
  -> relay
  -> edge control WebSocket
  -> parse and validate
  -> reserve command ID
  -> dispatch to navigation or action subsystem
  -> publish command statuses
  -> cache final result in command journal
```

### 4.2 Robot and navigation telemetry

```text
bbOS slam.pose + nav.state + nav.plan
  |-- normalize_robot_state()
  |     `-- robot_state: current pose, readiness, general status
  |
  `-- normalize_navigation()
        |-- derive_navigation_status()
        |-- normalize_goal()
        |-- normalize_path()
        `-- navigation: map, goal, authoritative path, route status
```

### 4.3 Video

```text
bbOS camera.head.jpeg
  -> camera reader thread
  -> one-frame latest-only queue
  -> optional resize/recompression
  -> separate relay video WebSocket
```

Video must never block the control connection.

## 5. Configuration

`config.py` loads deployment-specific values without embedding them in robot behavior.

```python
@dataclass(frozen=True)
class EdgeConfig:
    relay_ws_url: str
    room_id: str
    map_id: str
    camera_topic: str = "camera.head.jpeg"
    left_hand_camera_topic: str = "camera.left.jpeg"
    video_fps: float = 7.0
    command_ttl_ms: int = 6_000
    journal_capacity: int = 256
    navigation_snapshot_interval_s: float = 5.0


def load_config() -> EdgeConfig: ...
def validate_config(config: EdgeConfig) -> None: ...
```

Expected environment variables:

```text
RELAY_WS_URL=wss://relay.example.com
ROOM_ID=demo-bot
MAP_ID=small-house
CAMERA_TOPIC=camera.head.jpeg
LEFT_HAND_CAMERA_TOPIC=camera.left.jpeg
VIDEO_FPS=7
```

Validation must fail early for missing identifiers, invalid URLs, or unreasonable numeric values.

## 6. Protocol contracts

### 6.1 Incoming command

```json
{
  "type": "command",
  "commandId": "unique-stable-id",
  "createdAt": 1789812000000,
  "expiresAt": 1789812006000,
  "action": "move_to",
  "payload": {
    "x": 2.4,
    "y": -1.1,
    "heading": 1.57
  }
}
```

Supported actions:

- `move_to`
- `move_to_view`
- `stop`
- `use_action`
- `free_cam_start`
- `free_cam_pose`
- `free_cam_stop`

```python
@dataclass(frozen=True)
class Command:
    command_id: str
    action: Literal[
        "move_to",
        "move_to_view",
        "stop",
        "use_action",
        "free_cam_start",
        "free_cam_pose",
        "free_cam_stop",
    ]
    created_at: int
    expires_at: int
    payload: dict[str, Any]


def parse_command(raw: str) -> Command: ...
def validate_command(command: Command, now_ms: int) -> None: ...
```

Validation rejects malformed IDs, unsupported actions, expired commands, invalid coordinates, unsupported action names, and movement while navigation is not ready.

### 6.2 Command status

```json
{
  "type": "command_status",
  "commandId": "unique-stable-id",
  "status": "executing",
  "detail": "optional human-readable detail"
}
```

Lifecycle:

```text
sent -> delivered -> accepted -> executing -> succeeded
                      |             |
                      |             `-> failed
                      `----------------> rejected
sent ----------------------------------> expired
```

```python
def make_command_status(
    command: Command,
    status: str,
    detail: str | None = None,
) -> dict[str, Any]: ...
```

### 6.3 Robot state

Robot state is the frequently changing pose and general status stream.

```json
{
  "type": "robot_state",
  "at": 1789812000123,
  "ready": true,
  "status": "navigating",
  "pose": {
    "x": 2.4,
    "y": 1.8,
    "heading": 0.7
  }
}
```

```python
def normalize_robot_state(nav_state: dict[str, Any], now_ms: int) -> dict[str, Any]: ...
```

The local navigation fields map as follows:

| Local navigation | Remote robot state |
|---|---|
| `ready` | `ready` |
| `status` | `status` |
| `rx` | `pose.x` |
| `ry` | `pose.y` |
| `rh` | `pose.heading` |

### 6.4 Navigation telemetry

Navigation telemetry contains the authoritative route and changes less frequently than pose.

```json
{
  "type": "navigation",
  "status": "moving",
  "mapId": "small-house",
  "mapRevision": "17",
  "goal": { "x": 5.2, "y": 3.1 },
  "path": [
    { "x": 2.4, "y": 1.8 },
    { "x": 3.1, "y": 2.0 },
    { "x": 5.2, "y": 3.1 }
  ]
}
```

```python
@dataclass(frozen=True)
class NavigationTelemetry:
    status: Literal["idle", "planning", "moving", "replanning", "arrived", "failed"]
    map_id: str
    map_revision: str
    goal: dict[str, float] | None
    path: list[dict[str, float]]


def normalize_goal(nav_state: dict[str, Any]) -> dict[str, float] | None: ...
def normalize_path(nav_state: dict[str, Any]) -> list[dict[str, float]]: ...
def derive_navigation_status(
    nav_state: dict[str, Any],
    previous_state: dict[str, Any] | None,
) -> str: ...
def make_navigation_event(nav_state: dict[str, Any], map_id: str) -> dict[str, Any]: ...
```

The local path is encoded as parallel coordinate arrays:

```json
{
  "path": [
    [2.4, 3.1, 5.2],
    [1.8, 2.0, 3.1]
  ]
}
```

The edge converts it to browser points by zipping the arrays:

```python
def normalize_path(nav_state: dict[str, Any]) -> list[dict[str, float]]:
    path = nav_state.get("path")
    if not path or len(path) != 2:
        return []
    xs, ys = path
    return [
        {"x": float(x), "y": float(y)}
        for x, y in zip(xs, ys, strict=False)
    ]
```

Other field mappings:

| Local navigation | Remote navigation |
|---|---|
| `gx`, `gy` | `goal.x`, `goal.y` |
| `path` | `path` point objects |
| `map_gen` | `mapRevision` |
| configured `MAP_ID` | `mapId` |

## 7. Navigation status state machine

Do not expose arbitrary internal status strings as the frontend's navigation state. Derive a small stable state machine:

```text
idle
  -> planning       move command accepted
  -> moving         authoritative path available
  -> replanning     path disappears or changes during active movement
  -> moving         replacement path available
  -> arrived        navigation stops near the goal
  -> failed         navigation stops far from the goal or times out
```

Suggested interpretation:

| Condition | Remote status |
|---|---|
| Navigation not ready and no active command | `idle` |
| Goal exists, running, but no path | `planning` |
| Running with a path | `moving` |
| Previously moving but active path is temporarily absent or invalidated | `replanning` |
| Stopped within arrival tolerance | `arrived` |
| Stopped or timed out outside arrival tolerance | `failed` |
| Explicit Stop completed | `idle` |

The same state machine should help finish the related `move_to` command. `arrived` produces `succeeded`; a terminal failure produces `failed`.

## 8. Navigation publisher

Do not resend the complete route with every pose update.

```python
class NavigationPublisher:
    def build_event(self, nav_state: dict[str, Any]) -> dict[str, Any]: ...
    def fingerprint(self, event: dict[str, Any]) -> str: ...
    def has_changed(self, event: dict[str, Any]) -> bool: ...
    async def publish_if_changed(self, event: dict[str, Any]) -> None: ...
    async def publish_snapshot_if_due(self) -> None: ...
```

The change fingerprint covers:

- Navigation status.
- Map ID and revision.
- Goal.
- Authoritative path.

Pose is excluded because it is already published in `robot_state`. The edge should also republish a navigation snapshot periodically so clients can recover from a missed event.

## 9. Command journal and effective-once execution

Network delivery is at least once. Stable command IDs plus robot-side deduplication make execution effectively once.

```python
class CommandJournal:
    def get(self, command_id: str) -> dict[str, Any] | None: ...
    def reserve(self, command: Command) -> bool: ...
    def update(self, command_id: str, result: dict[str, Any]) -> None: ...
    def prune(self, now_ms: int) -> None: ...
```

The ID must be reserved before the first asynchronous pause:

```python
async def handle_command(command: Command) -> None:
    previous = journal.get(command.command_id)
    if previous is not None:
        await send_to_relay(previous)
        return

    journal.reserve(command)
    await report(command, "delivered")
    validate_command(command, now_ms())
    await report(command, "accepted")
    await dispatch_command(command)
```

For the hackathon, the journal is bounded, in memory, and cleared when the process restarts.

## 10. Local navigation adapter

The deployed bbOS navigation daemon already exposes typed `nav.command`,
`nav.state`, and `nav.plan` topics. The adapter holds the exclusive command
writer for the lifetime of a route and reads navigation state; it does not send
commands through the diagnostic web UI or write `drive.ctrl`.

```python
class BbosNavigationAdapter:
    async def start(self, command_id: str, payload: dict[str, Any]) -> None: ...
    async def wait(self) -> None: ...
    async def stop(self, reason: str) -> None: ...
```

`start()` writes the route through the existing typed API:

```python
with Writer("nav.command", Type("nav_command"), keeptime=False) as command:
    with command.buf() as output:
        output["enabled"] = True
        output["num_waypoints"] = 1
        output["waypoints"][0] = (x, y, heading)
```

Completion requires more than publishing the route. The adapter waits for bbOS
to report `nav.state=reached`; `failed`, command-writer loss, Stop, or an
unexpected return to `idle` cannot be reported as success. bbOS owns arrival
tolerance, planning, stale-pose handling, drive acquisition, and base output.

## 11. Camera capture and video

bbOS already exposes encoded JPEG data through `Reader("camera.head.jpeg")`.

It also exposes the left-hand camera through `camera.left.jpeg`. The edge owns the active
video-source selection; the browser continues receiving frames through the same relay video socket
regardless of whether the head or left-hand camera is active.

```python
def camera_reader(
    topic: str,
    output: Queue[bytes],
    stop_event: threading.Event,
) -> None: ...

def put_latest(queue: Queue[bytes], frame: bytes) -> None: ...
def resize_jpeg(frame: bytes, width: int, height: int, quality: int) -> bytes: ...
async def next_frame(queue: Queue[bytes]) -> bytes: ...
```

The queue should hold at most one or two frames. When full, discard the old frame before inserting the new one. A current frame is more useful than smooth but stale video.

Start by sending the existing JPEG at 5-10 FPS. Add resizing only if the native image is too large for the network or browser.

## 12. Relay connections

Use independent outbound WebSockets:

```text
Control socket: commands, acknowledgements, robot state, navigation
Video socket:   JPEG frames only
```

```python
async def run_reconnecting(name: str, session_factory: Callable[..., Awaitable[None]]) -> None: ...
async def control_session(agent: EdgeAgent) -> None: ...
async def receive_commands(socket: Any, agent: EdgeAgent) -> None: ...
async def publish_robot_state(socket: Any, agent: EdgeAgent) -> None: ...
async def publish_navigation(socket: Any, agent: EdgeAgent) -> None: ...
async def video_session(agent: EdgeAgent) -> None: ...
async def publish_video(socket: Any, frames: Queue[bytes], fps: float) -> None: ...
```

Both sessions need ping/pong, exponential reconnect backoff, independent failure handling, and a bounded maximum delay such as eight seconds.

## 13. Relay snapshot caching

The relay must remember both the latest robot state and the latest navigation event so a newly joined browser immediately receives a coherent snapshot.

```python
@dataclass
class Room:
    robot: WebSocket | None = None
    robot_video: WebSocket | None = None
    clients: list[WebSocket] = field(default_factory=list)
    client_videos: list[WebSocket] = field(default_factory=list)
    last_state: str | None = None
    last_navigation: str | None = None
```

Robot message handling:

```python
if parsed.get("type") == "robot_state":
    room.last_state = message
elif parsed.get("type") == "navigation":
    room.last_navigation = message
```

Browser connection handling:

```python
if room.last_state:
    await send_text(socket, room.last_state)
if room.last_navigation:
    await send_text(socket, room.last_navigation)
```

## 14. Command dispatch and Stop priority

```python
async def dispatch_command(command: Command) -> None:
    match command.action:
        case "move_to":
            await execute_move_to(command)
        case "move_to_view":
            await execute_move_to_view(command)
        case "stop":
            await execute_stop(command)
        case "use_action":
            await execute_action(command)
        case "free_cam_start":
            await execute_free_cam_start(command)
        case "free_cam_pose":
            await execute_free_cam_pose(command)
        case "free_cam_stop":
            await execute_free_cam_stop(command)
```

```python
async def execute_move_to(command: Command) -> None: ...
async def execute_move_to_view(command: Command) -> None: ...
async def execute_stop(command: Command) -> None: ...
async def execute_action(command: Command) -> None: ...
```

Stop must bypass the normal motion-command lock. It must immediately send the local navigation Stop command, cancel the edge task waiting for movement completion, mark the interrupted command appropriately, and acknowledge Stop. It must continue working even if video or the action subsystem has failed.

Stop also terminates an active Free Cam session, freezes or safely parks the controlled arm, and
returns video to the head camera. It must not wait for the normal Free Cam command queue.

## 15. Free Cam hand-camera mode

Free Cam is an exclusive edge-owned control mode for the left hand. The browser sends bounded,
absolute view targets; it never sends joint angles, velocities, torques, or IK results.

### 15.1 Start and stop contracts

```json
{
  "action": "free_cam_start",
  "payload": {
    "sessionId": "browser-generated-uuid"
  }
}
```

Starting the mode is one atomic edge operation:

1. Stop local navigation and clear the active navigation goal/path.
2. Confirm wheel velocity is zero and hold the mobile base stopped.
3. Reject the request if another behavior owns the left arm or if the left arm is unhealthy.
4. Acquire exclusive ownership of the left arm.
5. Move it to a known collision-checked Free Cam neutral pose at a limited speed.
6. Switch the outgoing video source to `camera.left.jpeg`.
7. Report `succeeded`; only then does the frontend enable look controls.

```json
{
  "action": "free_cam_stop",
  "payload": {
    "sessionId": "browser-generated-uuid"
  }
}
```

Stopping returns the arm to its safe neutral or parked pose, releases arm ownership, switches video
back to `camera.head.jpeg`, and only then reports success. It does not automatically resume the
navigation command that was interrupted when Free Cam started.

### 15.2 Pose updates

```json
{
  "action": "free_cam_pose",
  "payload": {
    "sessionId": "browser-generated-uuid",
    "panDeg": 15,
    "tiltDeg": -5,
    "sequence": 4
  }
}
```

`panDeg` and `tiltDeg` are absolute offsets from the left hand's Free Cam neutral pose. The
frontend currently clamps pan to `[-60, 60]` degrees and tilt to `[-35, 45]` degrees. The edge must
apply its own equal or tighter limits, solve IK locally, collision-check the complete trajectory,
and enforce position, velocity, acceleration, and torque limits.

Every session has a monotonically increasing `sequence`. The edge records the highest applied
sequence and acknowledges but ignores an older or repeated pose. This prevents a delayed retry from
moving the camera back to a stale orientation. Only the active `sessionId` is accepted.

```python
class FreeCamController:
    async def start(self, session_id: str) -> None: ...
    async def set_pose(
        self,
        session_id: str,
        sequence: int,
        pan_deg: float,
        tilt_deg: float,
    ) -> None: ...
    async def stop(self, session_id: str, park: bool = True) -> None: ...
    async def emergency_stop(self) -> None: ...
```

### 15.3 Safety and liveness

- Base-motion commands are rejected while Free Cam owns an arm.
- Arm actions are rejected while Free Cam is active.
- The right arm remains in its existing safe state and is never implicitly acquired.
- A browser disconnect or absence of valid Free Cam commands for a short watchdog interval exits
  the mode and parks or safely freezes the arm.
- IK failure, stale arm state, collision risk, camera loss, or actuator fault immediately stops arm
  movement and reports failure.
- The camera source changes only at session boundaries, never on individual pose updates.
- A process shutdown invokes the same emergency stop and arm-release path.

## 16. Camera-click projection

`move_to_view` converts a normalized camera click into a navigation coordinate.

```json
{
  "action": "move_to_view",
  "payload": {
    "u": 0.63,
    "v": 0.78,
    "coordinateSpace": "normalized_camera"
  }
}
```

Required functions:

```python
async def view_to_nav_target(
    u: float,
    v: float,
    frame: SensorFrame,
    robot_pose: RobotPose,
    calibration: CameraCalibration,
) -> tuple[float, float]: ...

def normalized_to_pixel(u: float, v: float, width: int, height: int) -> tuple[int, int]: ...
def sample_depth(depth: np.ndarray, px: int, py: int, radius: int = 3) -> float: ...
def deproject_pixel(
    px: int,
    py: int,
    depth_m: float,
    intrinsics: CameraIntrinsics,
) -> np.ndarray: ...
def camera_to_nav(
    point_camera: np.ndarray,
    camera_extrinsics: np.ndarray,
    robot_pose: RobotPose,
) -> np.ndarray: ...
def validate_nav_target(target: np.ndarray) -> None: ...
```

Processing stages:

1. Convert normalized `u`, `v` into image pixels.
2. Sample a small neighborhood in the depth image and use a robust value such as the median.
3. Deproject the pixel and depth into a camera-frame 3D point.
4. Transform the point through camera, robot, and navigation frames.
5. Confirm that the result represents reachable ground.
6. Pass the resulting `x`, `y` to `move_to()`.

Reject missing or stale depth, points behind the robot, unreasonable distance, targets outside navigation bounds, and points that do not represent reachable floor.

The current video protocol sends bare JPEG bytes without a capture ID. For the first demo, use the latest depth only while the robot is stationary. A later protocol revision should attach a frame ID or capture timestamp so a click can be matched with the correct depth and pose.

## 17. Approved action registry

Only explicitly registered behaviors may be invoked remotely.

```python
ACTION_REGISTRY = {
    "wave": play_wave,
}


def list_actions() -> list[str]: ...
async def run_action(name: str) -> None: ...
async def stop_active_action() -> None: ...
async def play_wave() -> None: ...
```

`wave` is the preferred initial action because the repository already contains the movement data and playback logic. Extract the reusable movement player instead of importing the entire greeter application and its detection, Gemini, audio, and web dependencies.

The frontend currently uses `demo_action`. Either update it to send `wave` or temporarily map both names to the same implementation.

## 18. Occupancy map strategy

The `navigation` event identifies a map and revision but does not contain occupancy cells.

### 18.1 Hackathon choice: preset grid

```text
mapId -> browser loads committed map.grid.json
mapRevision -> informational and cache/version metadata
```

This requires no additional edge map transport and is the recommended initial approach.

### 18.2 Later choice: live SLAM grid

If the minimap must update while the robot explores, introduce a separate message containing map metadata and compressed cells:

```json
{
  "type": "navigation_map",
  "mapId": "small-house",
  "mapRevision": "17",
  "resolution": 0.05,
  "width": 320,
  "height": 240,
  "origin": { "x": -8.0, "y": -6.0 },
  "encoding": "gzip-base64",
  "cells": "..."
}
```

Large map updates should use a separate heavy-data connection so they cannot delay control or Stop.

## 19. Coordinate contract

Pose, route, goal, and occupancy grid must share a documented coordinate system:

- Navigation coordinates are measured in metres.
- Pose, goal, and path use the same navigation/SLAM frame.
- Heading is measured in radians.
- Map origin and resolution are part of the map metadata.
- The frontend documents whether it flips the vertical axis for rendering.
- Camera projection explicitly transforms camera -> robot -> navigation coordinates.

A shared fixture should be used by edge and frontend tests:

```json
{
  "pose": { "x": 2.4, "y": 1.8, "heading": 0.0 },
  "goal": { "x": 5.2, "y": 3.1 },
  "path": [
    { "x": 2.4, "y": 1.8 },
    { "x": 3.1, "y": 2.0 },
    { "x": 5.2, "y": 3.1 }
  ]
}
```

The frontend test must confirm that the route begins at the robot and ends at the goal in the correct map locations, without mirroring or unintended offsets.

## 20. Simulator updates

The simulator should publish navigation events so the minimap can be tested without hardware.

```python
async def emit_navigation(self, socket: Any) -> None: ...
def simulated_path(self) -> list[dict[str, float]]: ...
```

Expected simulated sequence:

```text
Command accepted -> planning
Path created     -> moving
Robot advances   -> moving with authoritative path
Destination hit  -> arrived
Stop received    -> idle with path cleared
```

The relay integration test should also confirm that a browser joining mid-route receives the cached pose and navigation snapshot immediately.

## 21. Main process and supervision

```python
async def main() -> None:
    config = load_config()
    validate_config(config)
    agent = EdgeAgent(config)
    await agent.start()
```

```python
class EdgeAgent:
    async def start(self) -> None: ...
    async def handle_command(self, raw: str) -> None: ...
    async def dispatch(self, command: Command) -> None: ...
    async def publish_state(self, state: dict[str, Any]) -> None: ...
    async def publish_navigation(self, navigation: dict[str, Any]) -> None: ...
    async def shutdown(self) -> None: ...
```

The main tasks run concurrently:

```python
await asyncio.gather(
    run_reconnecting("control", agent.control_session),
    run_reconnecting("video", agent.video_session),
)
```

On shutdown, the adapter writes `nav.command.enabled=false` and releases the
command writer. The bbOS navigation daemon independently stops when that writer
disconnects.

## 22. Testing plan

### 22.1 Protocol tests

- Accept a valid command.
- Reject missing IDs and unsupported actions.
- Reject expired commands.
- Reject invalid or non-finite coordinates.
- Serialize command, robot-state, and navigation events exactly as expected by the frontend.
- Validate Free Cam session IDs, absolute pose bounds, and sequence numbers.

### 22.2 Journal tests

- A repeated command ID does not execute twice.
- A retry returns the latest known result.
- Reservation occurs before dispatch.
- Old entries are pruned and capacity remains bounded.

### 22.3 Navigation adapter tests

- `move_to` sends `clear`, `add_wp`, and `start` in order.
- Stop is sent immediately.
- Arrival requires an appropriate local state transition and distance tolerance.
- Timeout and stopped-far-from-goal cases fail correctly.

### 22.4 Navigation telemetry tests

- Parallel local path arrays become ordered `{x, y}` points.
- Missing paths become an empty array.
- Goal and map revision are normalized correctly.
- Status transitions cover planning, moving, replanning, arrived, and failed.
- Pose-only changes do not resend the entire navigation route.

### 22.5 Projection tests

- Normalize image coordinates correctly.
- Reject out-of-range clicks.
- Handle missing, invalid, and noisy depth.
- Verify camera-to-navigation transforms against known calibration fixtures.
- Reject targets outside safe range or bounds.

### 22.6 Free Cam tests

- Starting Free Cam stops navigation before acquiring the arm.
- The selected camera replaces the head camera on the existing video socket.
- Absolute pan/tilt targets are clamped and collision-checked on the edge.
- Duplicate or out-of-order sequences never replay stale arm motion.
- Navigation and arm actions are rejected while Free Cam is active.
- Stop, disconnect, watchdog expiry, camera failure, and process shutdown all release the arm safely.
- Exiting returns to the head camera and does not resume interrupted navigation.

### 22.7 Relay integration tests

- Browser -> relay -> edge -> fake navigation works end to end.
- Lost acknowledgement plus browser retry causes one execution.
- New clients receive cached `robot_state` and `navigation` snapshots.
- Video congestion does not delay Stop.
- Reconnect does not execute an expired command.

### 22.8 On-robot test order

1. Edge connects and reports pose/status.
2. Minimap displays the robot in the correct position and orientation.
3. Camera appears remotely.
4. Stop reaches local navigation.
5. A short `move_to` succeeds in a controlled area.
6. The authoritative path and destination appear on the minimap.
7. Duplicate command IDs do not repeat movement.
8. The wave action runs once.
9. Camera projection is checked with wheels disabled.
10. Full `move_to_view` is tested in a controlled area.
11. With wheels raised or otherwise secured, Free Cam locks the base and moves the left-hand camera
    through small bounded targets.
12. Stop, browser disconnect, and watchdog expiry each leave the selected arm and base safe.

## 23. Implementation phases

### Phase 1: Reliable control foundation

- Configuration and protocol types.
- Command validation and journal.
- Relay control connection and reconnection.
- Thin `nav.command`/`nav.state` adapter.
- `move_to` and priority Stop.
- Robot-state telemetry.
- Unit and fake-WebSocket tests.

### Phase 2: Navigation and minimap telemetry

- Goal and path normalization.
- Navigation status state machine.
- Change-detected publishing and periodic snapshots.
- Relay navigation caching.
- Simulator navigation output.
- Coordinate-contract tests.

### Phase 3: Video and action

- bbOS JPEG reader.
- Latest-only frame queue.
- Separate relay video connection.
- Extracted wave action and allowlist.

### Phase 4: Camera-click navigation

- Depth acquisition using bbOS `Reader(aligned_to=...)` synchronization.
- Calibration loading.
- Pixel deprojection and frame transforms.
- Reachability and safety validation.
- Stationary-robot demo flow.

### Phase 5: Free Cam

- Exclusive mode and resource ownership.
- Base lock and navigation cancellation.
- Hand-camera video-source switching.
- Safe neutral pose, edge-side IK, limits, and collision checking.
- Sequence deduplication and disconnect watchdog.
- Controlled on-robot tests for the left hand.

### Phase 6: Robot packaging

- bbOS autostart integration.
- Structured logs and basic health reporting.
- Clean shutdown behavior.
- On-robot smoke and network-interruption tests.

## 24. Decisions and open questions

The following should be resolved before or during implementation:

1. What browser-facing endpoint starts the session and returns a user-scoped
   LiveKit token, and what supported bbOS hook should carry namespaced RealBot
   messages? The robot-side token endpoint and existing tracks/topics are now
   recorded in `LIVEKIT_ARCHITECTURE.md`.
2. Is the visitor minimap using a committed preset grid or expected to receive live SLAM grid changes?
3. What exact `MAP_ID` corresponds to the robot's active navigation frame?
4. Which exact aligned camera stream should pointing use: decoded head RGB,
   rectified stereo, or an existing mapping output?
5. Which audited bbOS calibration revision should be stored with captures?
6. Should the initial remote action be named `wave`, or must the frontend retain `demo_action`?
7. What distance tolerance and timeout define successful arrival?
8. After the additional RealBot lease expires, which behavior adapters beyond
   navigation must be stopped or safely parked?
9. What exact neutral pose, workspace, and edge-side pan/tilt limits are safe for the left-hand camera?
10. Which existing process owns `arm_left.ctrl`, and what arbitration or handoff contract lets the
    edge acquire it without fighting another writer?
11. Should watchdog expiry hold the last safe pose or run a verified parking trajectory?

Reasonable hackathon defaults are one room, one controlling browser, a preset map, no persistence, no recording, a five-second navigation snapshot interval, a bounded in-memory journal, and local navigation continuing to own all motion safety.

## 25. Definition of done

The edge vertical slice is complete when:

- A robot and browser on different networks join the same authorized LiveKit room.
- The browser receives current pose, camera frames, map identity, goal, and authoritative path.
- The visitor minimap renders pose, heading, destination, route, and planning state correctly.
- `move_to` results in local navigation and a correct status lifecycle.
- Stop is handled with priority.
- The selected action executes through an allowlisted local implementation.
- A retried command ID never produces duplicate motion.
- A newly connected browser immediately receives cached robot and navigation snapshots.
- A brief disconnect reconnects without replaying expired commands.
- Camera-click navigation either passes the controlled on-robot test or remains explicitly disabled until its depth/calibration checks are satisfied.
- Free Cam holds the base stopped, exposes only bounded edge-validated left-arm movement, switches
  to the left-hand camera, rejects stale sequences, and exits safely on Stop or disconnect.
