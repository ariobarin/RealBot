# Bracket Bot SLAM telemetry

> **Status: observed implementation reference (2026-09-19).**
>
> This document describes the bbOS SLAM, mapping, navigation, and browser
> telemetry observed on `bracketbot-0187`. It is not a safety specification.
> The deployed robot and its bbOS checkout remain the source of truth.

## What “SLAM” means here

SLAM is simultaneous localization and mapping. On Bracket Bot it is not one
browser message or one occupancy-grid file. It is a pipeline:

1. The SLAM daemon feeds stereo RGB frames into `bbslam.so`.
2. It publishes the robot's corrected map-frame pose and its raw visual-odometry
   pose.
3. The mapping daemon aligns depth, RGB, and pose by capture timestamp and
   integrates them into a colored voxel map and a 2D navigation grid.
4. The navigation daemon plans paths over the 2D grid.
5. `nav/main.py` converts those bbOS topics into small realtime JSON messages
   and compressed binary visualization packets for a browser.

The web application should treat pose, map, and navigation as related but
distinct streams. A valid camera frame does not imply valid SLAM, and a running
mapping process does not imply that the map is current.

## Robot inspection snapshot

The following was observed read-only on `192.168.2.22` on 2026-09-19:

- `camera.depth` was live at `512 x 384`, using `uint16` millimetres. One sample
  contained 184,604 valid pixels (93.9%), with a 309 mm median.
- The SLAM process was not running, and `slam.pose` and `slam.health` were not
  present in shared memory.
- The last SLAM log ended with `KeyboardInterrupt`. Before stopping, visual
  odometry was lost and relocalization had not recovered.
- Mapping and navigation processes were still running, but
  `mapping.voxels.num_voxels` was zero and all 2,250,000 `mapping.grid2d` cells
  were unknown.
- Mapping had discarded more than 62,000 frames while `slam_lost=True`.
- The last retained rebuild event was PGO rebuild 1 at frame 9,633, with 19,184
  moved floor cells, zero emptied cells, and 5,470 filled cells. It was stale at
  inspection time and is not a current map.
- `nav.state` was `idle`, waypoint index `-1`, with reason
  `command writer disconnected`.

This is the correct empty/degraded state for frontend development: the UI must
show that depth is live while SLAM and map telemetry are unavailable. It must
not substitute fabricated geometry.

## Deployed-code caveat

The robot checkout was at commit `b890624`, but its `nav/main.py` had
uncommitted changes: 476 inserted lines and 1,530 deleted lines relative to
that commit. It also differs materially from the current public repository.

Before implementing or changing a protocol, compare against the deployed file.
A read-only snapshot from the inspection is kept outside this repository at
`../robot-inspection/nav-main-robot-0187.py`.

## bbOS topics

### SLAM

`slam.pose` is a realtime topic with a declared 33 ms period:

| Field | Type | Meaning |
| --- | --- | --- |
| `pos` | `float32[3]` | Corrected position in the SLAM map frame |
| `quat` | `float32[4]` | Corrected orientation quaternion |
| `vo_pos` | `float32[3]` | Raw visual-odometry position |
| `vo_quat` | `float32[4]` | Raw visual-odometry orientation |
| `pgo_count` | `int32` | Number of pose-graph optimizations applied |
| `timestamp` | bbOS timestamp | Capture time inherited from the camera frame |

`slam.health` contains:

| Field | Meaning |
| --- | --- |
| `degraded` | SLAM is operating in a degraded state |
| `stalled` | The camera-to-SLAM feed gap exceeded 300 ms |
| `vo_lost` | Visual odometry is lost; mapping must not integrate |
| `last_gap_ms` | Time since the previous fed camera frame |
| `localized` | The pose is anchored to a usable map frame |
| `relocalized` | A loaded map has been successfully relocalized |

`slam.history_generation` is a small change notification containing the
trajectory generation, pose count, and PGO count. Mapping uses it to reload the
corrected trajectory and rebuild affected map cells after loop closure.

### Mapping

`mapping.grid2d` is declared at 200 ms:

- `grid`: `uint8[1500,1500]`;
- resolution: `Config("mapping").voxel_size_m`, observed as 0.03 m;
- `origin`: world coordinate for the grid origin;
- `robot_pos` and `robot_heading`: the pose used for that publication;
- cell encoding: `0` unknown, `1` traversable floor, `2` obstacle.

`mapping.voxels` is declared at 500 ms and can contain up to one million
voxels:

- `num_voxels`;
- `coords`: `float32[max_voxels,3]` in SLAM world coordinates;
- `colors`: `uint8[max_voxels,3]`;
- `labels` and implementation-specific `info`;
- detected hole positions and metadata;
- grid origin and robot pose associated with the cloud.

The mapping daemon only integrates a depth frame when SLAM health is localized
and not lost, and when matching pose and RGB samples are available near the
depth timestamp.

`mapping.reproject` reports rebuild progress. `mapping.rebuild` reports the
landed rebuild count, frame, and moved/emptied/filled floor-cell changes.

### Navigation

`nav.state` is declared at 125 ms:

- `state`: `idle`, `waiting_for_drive`, `navigating`, `reached`, or `failed`;
- `waypoint_index`: active waypoint, or `-1`;
- `reason`: waiting or failure detail.

`nav.plan` is state published when a plan changes:

- up to 500 path points;
- a `1500 x 1500` float64 cost field;
- a `1500 x 1500` float32 clearance field.

The cost and clearance matrices are not suitable for repeated JSON transport.
`nav/main.py` samples the path and converts optional cost visualization into a
compressed display packet instead.

## Robot-hosted web protocol

The deployed `nav/main.py` separates realtime state from heavy visualization
data so a map transfer cannot block pose updates or commands.

### `/ws`: realtime JSON

The text WebSocket publishes state at approximately the `nav.state` rate. A
state message has `t: "state"` and may contain:

- `ready`, `status`, and reset state;
- `rx`, `ry`, and `rh` for robot x, y, and heading;
- goal `gx` and `gy`;
- active waypoint, route-running, loop, global-goal, and manual modes;
- waypoints, a path sampled to at most roughly 200 points, and controller
  prediction;
- floor, gradient, depth, SLAM-path, and nav-map display modes;
- `map_gen`, which changes when the live map must be cleared;
- map-rebuild summary and planner/controller diagnostics.

The same connection accepts local navigation and display commands. A cloud
bridge should expose only the intended high-level remote command subset rather
than forwarding arbitrary messages to this socket.

### `/heavy`: binary visualization packets

Every message begins with an eight-byte little-endian header:

```text
uint32 packet_type
uint32 item_count
zlib-compressed payload
```

| Type | Content | Browser behavior |
| --- | --- | --- |
| `2` | Raw traversable-floor `float32` XYZ points | Replace floor overlay |
| `3` | Planner heatmap: XYZ points followed by RGB bytes | Replace cost overlay |
| `4` | Progressive voxel keyframe chunk | Reset on `first=1`, then append chunks |
| `5` | Voxel upserts, removals, and upsert colors | Apply to current keyframe |
| `6` | Capture pose, pitch, and one or two depth images | Replace live depth cloud |
| `7` | Robot-radius-inflated floor XYZ points | Replace safe-floor overlay |

Type 4 payload:

```text
int32 base_x, base_y, base_z
float32 cell_resolution
uint32 first
uint16 cell_offsets[item_count][3]
uint8 colors[item_count][3]
```

Keyframes are split into chunks of at most 40,000 voxels. `first=1` means the
browser must discard its previous cloud before applying the chunk.

Type 5 payload:

```text
int32 base_x, base_y, base_z
float32 cell_resolution
uint32 upsert_count, removal_count
uint16 upsert_offsets[upsert_count][3]
uint16 removal_offsets[removal_count][3]
uint8 upsert_colors[upsert_count][3]
```

Voxel offsets are added to the signed base cell, then multiplied by
`cell_resolution`. A lost or skipped delta corrupts the browser's cloud state;
after queue overflow or reconnect, the server sends a fresh keyframe before
resuming deltas.

Type 6 begins with four float32 values—robot x, robot y, robot yaw, and IMU
pitch in radians—followed by a row-major `uint16` depth image in millimetres.
Raw-display mode appends a second same-sized unfiltered image.

`GET /depth_calib` provides `fx`, `fy`, `cx`, `cy`, image dimensions,
camera-to-base transform, and maximum display depth for client-side
unprojection.

## Frontend integration plan

### 1. Add an explicit SLAM telemetry model

Do not overload the current simplified `RobotState` with map state. Keep:

- realtime robot pose and command state;
- SLAM health and freshness;
- map generation and rebuild state;
- occupancy/voxel/depth buffers;
- navigation goal and route.

Each stream needs an observed timestamp and an independently derived stale
state. “Robot online” must not imply “SLAM ready.”

### 2. Preserve transport priority

Use separate relay lanes for realtime control/state and heavy map data. Video
should remain separate as well. Stop and acknowledgements must never queue
behind video, voxel keyframes, or depth frames.

### 3. Decode heavy packets outside React

Implement the packet decoder in a Web Worker. Transfer typed-array buffers back
to the rendering layer and update Three.js `BufferGeometry` imperatively.
Parsing or diffing a million-point cloud in a React render will stall pose and
command feedback.

### 4. Make resynchronization explicit

Track:

- connection generation;
- `map_gen`;
- whether a complete voxel-keyframe reset has begun;
- the last accepted delta sequence if the relay adds sequencing.

Never apply deltas from a previous connection or map generation. The cloud
should remain “loading” until a fresh keyframe reset arrives.

The current local binary format has no application-level sequence number.
When relaying across the internet, add an envelope containing room, map
generation, packet sequence, capture timestamp, and packet bytes. This lets the
browser detect a missing delta instead of silently displaying a corrupt map.

### 5. Render truthfully

The current committed PGM-derived maps may remain a clearly labelled preset or
offline-map mode. Live mode should render only actual data:

- no `slam.pose`: show SLAM unavailable;
- `vo_lost` or not localized: freeze or hide live map integration and explain
  why;
- keyframe in progress: show progressive map loading;
- stale grid/voxels: retain the last frame only with an explicit stale age;
- no voxels or all-unknown grid: show an empty-map state, not demo geometry.

### 6. Record fixtures from the robot

After SLAM is running and localized, record a short sanitized session containing:

- realtime `/ws` messages;
- `/heavy` keyframe chunks and deltas;
- one floor, heatmap, and depth packet;
- a loop-closure/rebuild transition if practical;
- disconnect and keyframe-resync behavior.

Replay those exact bytes in frontend tests. Synthetic tests remain useful for
malformed packets and boundary sizes, but product screenshots and demos should
use captured robot data or be clearly labelled schematic.

## Acceptance checks

- Pose continues updating while a voxel keyframe is loading.
- A dropped delta forces resynchronization rather than silently diverging.
- Map generation changes clear the previous cloud, floor, route, and trail.
- SLAM lost, unlocalized, stalled, stale, and offline are distinct UI states.
- Axis conversion is consistent: navigation `(x, y, z-up)` maps to Three.js
  `(x, z, -y)`.
- A recorded pose, route, floor cell, and voxel align in the same location.
- New viewers receive a full keyframe before deltas.
- Camera or depth availability does not mark SLAM ready.
- Heavy-map congestion does not delay Stop, state, or command acknowledgements.
