# RealBot hackathon relay

Small in-memory WebSocket relay plus a simulated robot. This is a local
protocol test harness, not the selected hardware transport or a production
device gateway. Real hardware sessions will use the existing bbOS LiveKit
`remote_session`; see [`../docs/LIVEKIT_ARCHITECTURE.md`](../docs/LIVEKIT_ARCHITECTURE.md).

The current relay does not authenticate browser or robot sockets; knowing a room
ID is sufficient to join. The planned browser-side boundary is documented in
[`../docs/REALTOR_GUEST_AUTH_PLAN.md`](../docs/REALTOR_GUEST_AUTH_PLAN.md):
Supabase-authenticated realtors and accountless guest invitations exchange their
sessions for short-lived socket tickets before joining `/ws/client/...`. That
work intentionally leaves `/ws/robot/...` authentication unchanged and out of
scope.

```sh
cd relay
uv sync
uv run uvicorn app:app --reload --port 8000
```

In another terminal:

```sh
cd relay
uv run python simulator.py
```

The simulator joins room `demo-bot` by default. Configure it with
`RELAY_WS_URL` and `ROOM_ID`. Run tests with `uv run pytest`.

Camera-click navigation uses `move_to_view` rather than pretending a browser pixel is a SLAM
coordinate. Its payload contains normalized `u` and `v` values in the range `0..1` and
`coordinateSpace: "normalized_camera"`. The simulator applies a deliberately rough conversion;
the hardware adapter must resolve the camera ray to reachable ground using depth or calibrated
camera geometry before handing the target to navigation.

A room supports one robot connection and multiple simultaneous browser viewers, allowing a
realtor dashboard and the end-user view to observe and control the same session without replacing
each other's WebSockets.

The first browser receives the demo control lease; later browsers are view-only.
The controller sends a one-second heartbeat, and disconnect immediately emits
`control_lost` to the robot. Stop is accepted from any viewer. The RealBot
session guard and workflow coordination live in [`../edge_agent`](../edge_agent),
while bbOS remains authoritative for topic ownership, navigation, command
freshness, and hardware safety. The simulator uses the RealBot package with fake
adapters.

The deployed robot already has a bbOS `remote_session` daemon using LiveKit for
camera, microphone, browser/Quest teleoperation, disconnect teardown, and arm
parking. RealBot has selected that transport. Do not point this relay at robot
hardware or run it as a second camera/control owner. Keep it for browser UI,
protocol, retry, lease, and fake-adapter tests.

For a networked demo, configure separate high-entropy credentials:

```text
ROBOT_TOKEN=<robot-only secret>
CONTROL_TOKEN=<controlling-browser secret>
VIEW_TOKEN=<view-only browser secret>
```

Set `VITE_RELAY_TOKEN` in the controlling web deployment and `ROBOT_TOKEN` for
the simulator or robot edge process. If the relay token variables are omitted,
the local-development behavior remains open and the first browser gets control.
Credentials are sent as the first WebSocket message and are not placed in URLs.
