# Read-only LiveKit telemetry milestone

## Status

Implemented locally: bbOS telemetry reader, bounded 5 Hz LiveKit publisher,
realtor-only browser viewer, token-endpoint client, development viewer-token
entry, and automated tests. The browser uses the existing robot's `cam-wrist`
track (the head-camera queue in the audited daemon).

Not yet deployed: the bbOS session hook and the browser session/token backend.
No robot camera or cross-network LiveKit connection has been claimed as tested.

On 2026-09-19 the new reader was run on bracketbot-0187 over SSH without writing
files or starting daemons. Three snapshots correctly reported absent SLAM and
navigation data. `slam`, `nav`, and `remote_session` all had `.stopped` markers;
there were no active pose/health/nav-state shared-memory topics. These explicit
stop settings were left intact.

## Browser

From the realtor dashboard select **Live robot view**, or open
`/realtor/live/<roomId>` after signing in. It shows session connection separately
from expected robot presence, telemetry freshness, SLAM status, pose in map
metres, heading in radians, navigation state/reason, and map identity when supplied.
There are no motion, microphone, arm, or action commands on this page. Stale
telemetry becomes unavailable after two seconds without a new snapshot.

Set `VITE_LIVEKIT_SESSION_ENDPOINT` to an application endpoint implementing:

```text
POST <configured endpoint>
Authorization: Bearer <Supabase user access token>
Content-Type: application/json

{"roomId":"authorized-room","mode":"view"}
```

```json
{
  "url": "wss://your-livekit-server",
  "token": "short-lived-user-scoped-viewer-token",
  "robotIdentity": "exact-livekit-identity-of-the-robot"
}
```

This is a proposed RealBot application endpoint contract, not a claim that
Bracket Bot's existing robot token endpoint accepts Supabase access tokens.
The endpoint must validate the user, organization, robot/room access and issue
room-specific grants with `canSubscribe=true`, `canPublish=false`, and
`canPublishData=false`. The robot identity must come from trusted device/session
records. The browser rejects media/data from other identities. Grant enforcement
must happen on the server; the client page itself is not an authorization boundary.
The API must allow the web origin if hosted separately, and return `Cache-Control:
no-store`. Neither LiveKit signing secrets nor `/etc/BB_API_KEY` belong in Vite
configuration, the browser, or logs.

In development only, leaving the endpoint unset exposes a URL, robot-identity,
and short-lived viewer-token form for an existing room. Tokens are kept in memory
and the input is cleared when joining. Production builds require the endpoint.
An explicit Connect avoids opening a robot session as a side effect of visiting
the page. Disconnect/reconnect never transmits commands.

## Robot hook

Install `edge_agent` in the existing bbOS `remote_session` Python environment
through its normal dependency/package management. Do not start a second LiveKit
participant, camera publisher, or hardware writer. In that daemon's `run_session`
task list, after `room.connect` succeeds, add:

```python
from edge_agent.telemetry import publish_telemetry

# Add alongside publish_feed/publish_mic in the existing session tasks list:
asyncio.create_task(publish_telemetry(room)),
```

Use the existing daemon task cancellation and teardown scope. The coroutine
closes its readers on cancellation and propagates a publish timeout after two
seconds to the host's existing task-failure handling. It does not alter bbOS
teleoperation, participant-disconnect policy, or hardware control.

The deployment change must be made in the bbOS repository, which is not part of
RealBot. It has deliberately not been applied to the stopped robot daemons.
Restarting the existing remote-session daemon can acquire actuator writers even
for a viewer, so its startup must be coordinated with the robot operator.

For a read-only local smoke check in a configured bbOS environment:

```sh
python -m edge_agent.telemetry
```

## Wire contract

Data topic: `realbot.telemetry`. UTF-8 JSON, version 1, small full snapshots every
200 ms, reliable delivery. Full snapshots initialize late joiners without a
request packet. No incoming command handler is installed for this milestone.

Fields: `type=telemetry`, `version=1`, `at` (robot Unix milliseconds), `mapId`
(nullable), `ready`, nullable `pose` (`x,y,z,heading`), `slam` (`fresh`,
`poseFresh`, nullable `localized,degraded,stalled,vo_lost,relocalized`), and
`navigation` (`fresh,state,reason,waypointIndex`).

Readers: `slam.pose`, `slam.health`, `nav.state`, all with `keeptime=False`.
Source freshness thresholds: pose 300 ms (matching deployed nav), health 1 s,
navigation 2 s. Missing or dead writers, future timestamps, and stale samples
must never appear ready. Invalid coordinates produce no pose; no origin or
preset map is substituted. `ready` means healthy localized pose only, not
authorization or permission to move. Heading follows the deployed nav planar
quaternion convention; robot forward is base +Y.

The browser validates the packet and sender, rejects duplicate/out-of-order
source timestamps, and uses local monotonic receipt time for its two-second
staleness timer. It resets that ordering boundary after a room reconnect or
robot leave/rejoin. Browser and robot wall clocks need not be synchronized.
Map grids, paths, microphone playback, actions, and controls are later milestones.

## Acceptance test still required

1. Provision an authorized viewer session and install the publisher hook.
2. With the operator present, bring the required bbOS services online.
3. Put the browser on a phone hotspot while the robot uses its normal network.
4. Verify head video, robot identity, and pose/SLAM/nav values against bbOS.
5. Remove connectivity: telemetry must go stale within about 2.25 s and the page
   must not offer motion. Reconnect and require new snapshots.
6. Stop SLAM: pose/health must become unavailable even if video continues.
7. Leave/rejoin the viewer and verify the existing bbOS session teardown behavior.

SDK references: [LiveKit room events](https://docs.livekit.io/reference/client-sdk-js/types/RoomEventCallbacks.html)
and [track subscriptions](https://docs.livekit.io/guides/room/receive).
