# LiveKit architecture decision

> **Decision (2026-09-19):** RealBot will use the LiveKit transport already
> provided by bbOS `remote_session`. The custom WebSocket relay remains a local
> simulator and protocol test harness; it will not be deployed beside LiveKit
> as a second robot-control or camera stack.

## Why

The deployed robot already has a bbOS `remote_session` daemon that connects to
Bracket Bot's cloud API and LiveKit. It publishes the head, left-hand, and
right-hand cameras and microphone; accepts supported browser and Quest
teleoperation; and handles participant disconnect, zero motion, arm parking,
and emergency-stop cleanup. Reusing it avoids duplicate camera consumers,
competing IPC writers, and a second NAT traversal and reconnect implementation.

## Runtime boundary

```text
Browser
  |-- HTTPS -> session authorization, room token, saved spaces/interactables
  `-- LiveKit room
        |-- subscribed tracks <- robot camera and microphone
        |-- published audio  -> realtor speech, if required by the workflow
        `-- data messages   <-> commands, acknowledgements, and live telemetry
                                  |
bbOS remote_session + thin RealBot workflow bridge
  |-- nav.command/nav.state/nav.plan -> navigation
  |-- aligned camera/depth/pose      -> pointing evidence
  |-- SLAM and mapping topics        -> map and localization status
  `-- approved action adapters       -> pretrained interactions
```

bbOS remains authoritative for SLAM, planning, IPC writer ownership, command
freshness, base safety, arm safety, and motor control. The RealBot edge package
validates product commands, deduplicates command IDs, coordinates workflow
modes, and translates only into audited bbOS topic contracts.

## Channel ownership

| Information | Transport | Notes |
| --- | --- | --- |
| Camera and microphone | LiveKit media tracks | Subscribe to the existing robot publications; do not re-encode JPEGs in a second daemon. |
| Ephemeral command and status traffic | LiveKit data messages | Use explicit message topics and stable command IDs. Preserve expiry and deduplication at the robot. |
| High-rate, disposable telemetry | LiveKit lossy data where supported | Pose/path display may drop stale updates; always send a fresh snapshot. |
| Command lifecycle and Stop | LiveKit reliable data plus local safety | Acknowledgements do not replace bbOS timeouts, disconnect handling, or Stop. |
| Tokens, room authorization, saved spaces, interactables | HTTPS application API | Never put LiveKit API secrets in the browser or robot repository. |
| Local development | Existing `relay/` simulator | Maintains deterministic tests until a LiveKit test-room harness exists. |

## Required message contract

RealBot messages need a dedicated LiveKit data topic or equivalent namespace so
they cannot be confused with existing bbOS teleoperation packets. At minimum:

- `realbot.command`: validated high-level command envelope;
- `realbot.command_status`: delivered/accepted/executing/final lifecycle;
- `realbot.robot_state`: pose, mode, lease, localization, and fault summary;
- `realbot.navigation`: goal, path, map identity/revision, and planner state;
- `realbot.interactables`: setup phase, draft point, verification, and saved points;
- `realbot.heartbeat`: additional realtor workflow lease heartbeat.

Before implementing these topics, inspect and document the deployed
`remote_session` token request, room naming, participant identity, track names,
data-message schema, and extension point. Extend its supported interface or add
one thin cooperating bridge; do not patch around it with another hardware
owner.

## Verified deployed contract

A read-only inspection of `bracketbot-0187` on 2026-09-19 confirmed the
following implementation in
`/home/bracketbot/bbos/bbos/daemons/remote_session/`:

- the daemon polls `POST https://api.bracketbot.com/v1/teleop/poll` with its
  serial number and device bearer credential;
- after receiving `roomId`, it requests its robot LiveKit credentials from
  `POST /v1/livekit/token` with `{ "room": roomId }`;
- it publishes video tracks named `cam-left`, `cam-wrist`, and `cam-right`;
- it publishes the microphone as `robot-mic`;
- it accepts data topics `movement`, `manipulation`, and `quest_state`;
- it publishes reliable `capabilities` and `quest_haptic` data topics;
- incoming participant audio is already sent to the robot audio path; and
- any participant disconnect currently zeros browser drive input, disables
  manipulation, and ends the session.

The robot's `/v1/livekit/token` call uses `/etc/BB_API_KEY`. That credential is
device-only and must never be copied into the web app. The browser-side endpoint
that starts a teleop session and returns a user-scoped room token still needs to
be located or supplied by the Bracket Bot backend.

Unknown data topics are currently ignored. Because `remote_session` exits when
any participant disconnects, launching a second robot-side LiveKit participant
would add fragile lifecycle coupling even before considering duplicate identity
or IPC ownership. The preferred implementation is therefore a reviewed bbOS
extension that forwards the namespaced RealBot messages to a local workflow
interface and publishes the resulting telemetry from the existing participant.
If bbOS maintainers expose a supported plugin/hook instead, use that hook.

## Browser migration

1. Add the LiveKit browser SDK and a transport interface behind the current UI.
2. Obtain a short-lived room token from a server-side endpoint.
3. Connect using the returned LiveKit URL and token.
4. Select camera tracks by published source/name rather than assuming arrival order.
5. Move command/state traffic to the agreed RealBot data topics.
6. Keep the current mock and WebSocket simulator transports for deterministic tests.
7. Remove `VITE_RELAY_TOKEN` and the JPEG socket from deployable builds after the
   LiveKit path passes the cross-network test.

## Edge migration

1. Preserve the protocol validator, command journal, workflow coordinator,
   interactables state machine, and thin bbOS navigation adapter.
2. Replace the planned `relay_client` runtime with a LiveKit participant adapter.
3. Consume frames from the supported bbOS/LiveKit extension point without
   competing with `remote_session` for a camera or arm/base writer.
4. Publish normalized state and command results through the agreed data topics.
5. On participant loss or heartbeat expiry, stop RealBot workflow modes and rely
   on the existing bbOS safety chain for final enforcement.

## First integration checkpoint

The first hardware milestone is intentionally read-only: a browser on a phone
hotspot joins the same LiveKit room as the robot on property Wi-Fi, displays the
existing head-camera track, and receives pose/localization telemetry. Only after
that succeeds should `move_to`, Stop, interactables setup, or arm actions be
enabled.

The remaining blockers are the browser-side session/token endpoint and a safe
bbOS extension point for namespaced RealBot data. Until those are verified, the
web app must remain on its mock or simulator transport and must not claim
hardware connectivity.
