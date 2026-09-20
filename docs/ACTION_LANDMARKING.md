# Action Landmarking

This feature runs independently of person following, navigation, and physical
action testing.

## Flow

```text
IDLE
  -- stereo thumbs-up within 1 m for 500 ms --> RECORDING
  -- robot says "Starting action location recording"

RECORDING
  -- one pointing hand --> index-finger ray
  -- synchronized camera.depth --> first surface intersection
  -- aligned slam.pose --> metric SLAM-map XYZ
  -- microphone --> "light switch" or "electric box"
  -- stable point + allowed label --> SAVED

SAVED
  -- robot says "Action location recorded"
  -- release gesture + cooldown --> IDLE
```

The point and spoken label may arrive in either order. Recording times out after
15 seconds and never saves a partial result. SLAM must be localized and healthy;
RGB, depth, and pose timestamps must remain within 50 ms and 100 ms respectively.

## Action linkage

The spoken phrase resolves to an allowlisted action definition, not an arbitrary
string. The first definitions are:

| Action ID | Spoken phrase | Display label | Marker |
| --- | --- | --- | --- |
| `light_switch` | “light switch” | Light switch | amber lightbulb |
| `electric_box` | “electric box” | Electric box | blue electrical-box marker |

Every saved landmark carries `action_id`, the complete action definition, and its
definition version. Future action execution should resolve the policy by this ID;
it should never infer behavior from the display label.

## Output

Records are atomically stored in
`~/.local/share/realbot/action_landmarks.json`. Each record contains the label,
SLAM/base/camera coordinates, depth pixel, optional surface normal, pointing-ray
quality, synchronized sensor timestamps, map revision, and calibration revision.

The hand service exposes current state and saved records at `GET /actions`.

## Speech setup

Set `OPENROUTER_API_KEY` in `~/.config/realbot/speech.env` on the robot (mode
`600`, outside Git), and enable the bbOS microphone daemon. Speech uses
`google/gemini-2.5-flash` through OpenRouter's audio-input API. Only active
recording windows submit short WAV clips; idle audio is discarded. The listener
accepts only the two exact action IDs, retries request failures, and discards
responses belonging to a recording window that has ended. `/actions` reports
microphone readiness, recognition status, and the last recognized label.
Microphone capture continues during requests, with at most one request in flight.
The viewer keeps the accepted label visible separately from SLAM/pointing status.

## Display contract

The map renders each saved point at its SLAM-map XYZ position, with the action's
marker color and icon. Selecting it opens the display name, action ID, capture
confidence, map/calibration revision, and recorded/stale state. The live camera
view projects the same point only when the current camera pose and calibration
are fresh; otherwise it remains map-only. Draft, recorded, and stale points use
different opacity/ring treatments, and the marker is never silently converted to
a floor waypoint.
