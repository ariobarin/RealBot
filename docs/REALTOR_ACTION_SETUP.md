# Realtor action-point setup

Realtor setup uses the same control page and transport as visitors, with an
Action points panel. Open a saved map and choose **Set up action points**, or
choose a scanned room from **Add a space**. No pairing or scanning is simulated.

The panel reads the existing hand_tracking.py service: GET /actions for capture
state and saved records, GET /stream for the annotated camera. The robot still
owns gesture recognition, voice labels, validation and persistence. Opening the
panel does not start recording; the existing thumbs-up gesture does.

For a local preview, configure web/.env.local (not committed):

```
ACTION_POINTS_TARGET=http://192.168.2.27:8006
ACTION_POINTS_ROOM_ID=bracketbot-scan
```

Run the existing hand_tracking.py on the assigned robot and start Vite. The dev
server forwards only that room's actions and stream requests. Missing service,
malformed replies and disconnections clear live status and retry. Visitors do
not receive the setup panel.

Production needs an authenticated, room-authorized server route at
/api/action-points/:roomId/actions and /stream. Vite's local proxy is not deployed
by a static build and is not a production authorization boundary. Existing
LiveKit session configuration is still required for the shared driving view.

A downloaded map does not restore robot localization. Capture still requires
healthy live SLAM, synchronized depth and a microphone. Records carry live SLAM
coordinates and revision; this UI does not imply they are aligned to the saved
map, physically tested, or ready to execute. Map alignment and production routing
remain separate integration work.
