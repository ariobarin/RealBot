# Remote control direction — working notes

> **Status: non-authoritative working notes (2026-09-19).**
>
> This document is a convenient record of what we currently have, what we are
> considering, and the questions we still need to answer. It is not an API
> contract, safety specification, delivery commitment, or source of truth.
> Running robot behavior, deployed services, validated interfaces, and current
> code take precedence. Expect this document to become incomplete or stale.

## How to read this document

- **Current** describes behavior observed in the repository today.
- **Direction** describes the approach we presently prefer, not a commitment.
- **Open question** marks something that must be decided or verified before it
  can be treated as a requirement.

## Current priority: hackathon proof

The immediate goal is a working cross-network demonstration, not a
production-ready device platform. For this phase, speed of implementation and
ease of debugging matter more than comprehensive authentication, durable
storage, multi-tenant permissions, or a polished onboarding experience.

The intended demo is:

1. The robot and user are on different networks.
2. The robot connects outward to a public relay.
3. The browser joins the robot's demo room.
4. The user sees camera frames and robot state.
5. The user sends `move_to`, `stop`, and one chosen `use_action` command.
6. Lost acknowledgements cause retries without duplicate execution.

The shortcuts below are deliberate hackathon choices, not recommendations for
a production deployment.

## Product intent

A user should be able to connect to their bracketbot from a different network,
watch its camera and live state, and send high-level intent such as:

- move to a map coordinate;
- use a named action;
- cancel or stop the current operation.

The robot remains responsible for perception, planning, collision avoidance,
motor control, and deciding whether a command is safe and executable. The user
interface should not normally send raw wheel velocities.

## What exists today

### Robot navigation service

**Current:** `nav/main.py` runs a FastAPI service on port `8010` and already has
two useful WebSocket boundaries:

- `/ws` carries small realtime state messages and accepts commands;
- `/heavy` carries compressed binary map, floor, depth, and voxel data.

Realtime state includes robot pose, readiness, status, waypoints, active
waypoint, planned path, and navigation modes. The accepted command set includes
`add_wp`, `start`, `stop`, `clear`, `remove_last`, `loop`, `teleop`, navigation
bounds, display/planner controls, and `wipe_map`.

The existing robot-hosted navigation UI reconnects to these sockets and keeps
heavy map traffic separate from realtime control traffic. Manual teleoperation
has a local timeout that stops motion when updates disappear.

### Realtor web application

**Current:** `web/` is a standalone Vite, React, and TypeScript application. It
currently provides:

- a library containing the Small House and TurtleBot3 Sandbox preset maps;
- a simulated onboarding and two-second pairing flow;
- a 2D/3D occupancy-grid renderer;
- a static robot marker and local waypoint markers;
- mock `add_wp` command feedback;
- map loading, invalid-map, and WebGL fallback states.

The displayed maps are committed PGM/YAML-derived presets. Pairing is simulated,
`MockBotClient` does not contact a robot, and placing a waypoint does not move
hardware. The web application does not yet implement user authentication,
device ownership, remote discovery, connection leases, live camera transport,
or a cloud-backed robot session.

## Hackathon architecture direction

Use a small public relay with two outbound WebSocket connections from the
robot: one for control/state and one for video frames. The browser connects to
the same relay and joins using a configured robot or room ID.

```text
Robot daemon  ── control WSS ──>  Public relay  <── control WSS ──  Browser
Robot camera  ─── video WSS ──>       │         <─── video WSS ──  Browser
                                       └── in-memory room pairing
```

For the hackathon, the relay may:

- keep connected robots, browsers, and pending sessions only in memory;
- allow one robot and one controlling browser in each room;
- use a random room ID or shared demo token instead of a full account system;
- forward messages without storing maps, commands, video, or audit history;
- forget the room when its connections close.

The robot daemon should bridge the existing local navigation service rather
than replace it. A minimal `move_to(x, y)` adapter may translate the high-level
command into `clear`, `add_wp`, and `start`. `stop` maps directly to the local
navigation Stop command. `use_action` may use a small hardcoded registry for the
single action selected for the demo.

### Hackathon video path

Unless the robot already exposes a working WebRTC or hardware-encoded stream,
send resized JPEG camera frames over the separate video WebSocket. A target
such as 640×360 at roughly 5–10 frames per second is acceptable for proving the
experience. Video frames may be dropped when the connection falls behind;
control messages may not be delayed behind video traffic.

WebRTC, STUN/TURN, an SFU, recording, and multi-viewer delivery are deferred
unless JPEG-over-WebSocket proves unusable on the available hardware/network.

### Hackathon reliability

- Commands use a stable `commandId`.
- The browser retries an unacknowledged command using that same ID.
- The robot daemon keeps a bounded in-memory set of recent IDs and results.
- A repeated ID returns the known status without executing the action again.
- Commands expire quickly and are not replayed after a long disconnect.
- `stop` has priority and is retried until acknowledged.
- No command database or durable journal is required for the demo.

## Possible post-hackathon direction

Robots are commonly behind NAT and firewalls, so the robot should not depend on
an inbound public connection. A daemon on the robot should establish and
maintain an outbound encrypted connection to a BracketBot cloud gateway.

```text
Robot agent  ── outbound WSS ──>  BracketBot Cloud  <── WSS ──  User browser
    │                                  │
    ├── local nav service              ├── authentication and ownership
    ├── camera source                   ├── online-device registry
    └── command journal                 └── session and command routing
```

**Later direction:** extend the existing cloud identity/device system if it is
a good fit, while keeping the realtime gateway independently deployable. The
robot agent should bridge the existing local navigation service rather than
replace the planner or motor-control stack.

### Robot agent

The contemplated robot daemon would:

- start with the robot and authenticate as a provisioned device;
- maintain an outbound WSS connection with heartbeat and reconnect backoff;
- report online status, capabilities, pose, map state, and action status;
- bridge authorized high-level commands to the local navigation service;
- publish camera media through a negotiated media connection;
- maintain a bounded journal of recent command IDs and results;
- reject expired, duplicated, unauthorized, or locally unsafe commands.

### Cloud gateway

The contemplated gateway would:

- authenticate devices and users;
- enforce device ownership and organization membership;
- advertise whether a robot is online;
- grant a time-bounded control lease to one controller;
- allow separately authorized view-only sessions if desired;
- route commands, acknowledgements, state, and WebRTC signaling;
- prioritize control and Stop traffic over mapping or media metadata;
- avoid replaying expired commands after reconnects.

### Browser client

The contemplated client would:

- list the signed-in user's robots and their connection state;
- open a control or view-only session through the cloud;
- display camera, pose, map, path, goal, and action status;
- send high-level commands rather than raw actuator commands;
- retry unacknowledged commands using the same command ID;
- reconcile its UI with authoritative robot state after reconnecting;
- keep Stop available whenever the robot may be moving.

## Possible post-hackathon media and control transport

**Direction:** keep media separate from control so video congestion cannot
delay Stop or other commands.

- Use WebRTC for encrypted, low-latency robot-to-user camera video.
- Use cloud-mediated signaling plus STUN, with TURN as the fallback when a
  direct peer connection cannot be established.
- Use WSS for sessions, commands, acknowledgements, robot state, map updates,
  and WebRTC signaling.
- Preserve separate priority classes for realtime state/control and heavy map
  or voxel data. Stale visualization data may be dropped; control may not be
  delayed behind it.

An SFU, recording, multi-viewer video, and server-side video analytics are not
assumed for the hackathon.

## Command delivery model

Network delivery is expected to be at-least-once. Action execution should be
effectively once through stable command IDs and robot-side deduplication.

For the hackathon, the acknowledgement state and recent-command journal may be
held entirely in robot-daemon memory. The fuller lifecycle below is a useful
shape for messages, but the UI only needs enough states to make delivery and
execution understandable during the demo.

Illustrative command envelope:

```json
{
  "commandId": "unique-stable-id",
  "sessionId": "active-control-session",
  "createdAt": 1789812000000,
  "expiresAt": 1789812005000,
  "type": "move_to",
  "payload": { "x": 2.4, "y": -1.1, "heading": 1.57 }
}
```

Illustrative lifecycle:

```text
sent -> delivered -> accepted -> executing -> succeeded
                      |             |
                      |             +--------> failed
                      +----------------------> rejected
sent ----------------------------------------> expired
```

- The sender retries when delivery or acceptance is not acknowledged.
- A retry uses the same `commandId`; it is not a new action.
- A robot that has already seen the ID returns the stored status and does not
  execute the action again.
- Commands have short expirations and are not blindly replayed after a long
  disconnect.
- `Stop` is high priority and may be sent repeatedly until acknowledged.
- Losing a teleoperation heartbeat or control lease must cause a local safe
  response, independent of the browser or cloud.

Exact retry intervals, persistence guarantees, acknowledgements, and safe-state
behavior remain design work rather than settled requirements.

## Hackathon vertical slice

The preferred proof is intentionally narrow:

1. Deploy a public in-memory WebSocket relay.
2. A robot on one network connects to a configured room and sends heartbeat.
3. A browser on another network joins the room and sees the robot online.
4. The browser receives JPEG camera frames and realtime robot state.
5. The user sends one `move_to` command and sees its status.
6. The user can send `stop` and one selected `use_action` command.
7. A deliberately lost acknowledgement causes a retry without duplicate motion.
8. A short network interruption reconnects without executing an expired command.

For the demo, `move_to` may be implemented by adapting to the existing waypoint
commands rather than adding a new planner API. The relay does not need a
database or account system.

This slice does not require general frontend polish, map editing, raw teleop,
planner tuning, recordings, multiple viewers/controllers, remote map wiping,
or production device provisioning.

## Critical hackathon questions

Only three discovery questions should block implementation:

- Where can the public WebSocket relay be deployed for the demo?
- How can the daemon obtain camera frames on the robot without conflicting with
  an existing camera consumer?
- Which single `use_action` behavior should the demo perform, and what local
  interface invokes it?

Reasonable defaults for everything else are one controller, one viewer, one
room ID, no persistence, no recording, a lightweight state stream, and no full
voxel stream.

## Later questions

These matter if the prototype becomes a product, but they do not block the
hackathon path:

- Which repository and service currently back `cloud.bracketbot.com`?
- What account, organization, and device-registry concepts already exist there?
- How is a new robot securely claimed by a user or organization?
- What credential can be provisioned on the robot at manufacture or setup?
- Should autonomous navigation continue when a production control session
  disconnects, or must it stop?
- Who may watch video, and may more than one viewer connect?
- How much live grid or voxel data is useful remotely relative to its bandwidth?
- Should the cloud retain state, commands, maps, video, or audit history?
- How should software updates and protocol-version compatibility be handled?

## Superseded broader vertical-slice notes

The earlier production-oriented sketch was:

1. A robot on one network connects outbound to a cloud gateway.
2. A user on another network sees that robot online and obtains control.
3. The user receives live camera video and realtime robot state.
4. The user sends one `move_to` command and sees its full status lifecycle.
5. A deliberately lost acknowledgement causes a retry without duplicate motion.
6. Stop interrupts the operation and receives priority handling.
7. A network interruption and reconnect restore authoritative robot state
   without executing an expired command.

This is retained only for context. The hackathon slice above is the current
priority.

## Explicitly not decided here

This document does not decide the final cloud technology, deployment topology,
authentication provider, database, message broker, WebRTC server, UI layout,
commercial permissions model, safety certification, or delivery schedule. It
also does not make older sprint labels or mock onboarding behavior permanent.

In particular, the hackathon's room-ID access, in-memory state, JPEG video, and
hardcoded action registry are disposable shortcuts rather than intended product
architecture.
