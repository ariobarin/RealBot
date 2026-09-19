# RealBot hackathon relay

Small in-memory WebSocket relay plus a simulated robot. This is a disposable
hackathon component, not a production device gateway.

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
