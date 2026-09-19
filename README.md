# RealBot

Bracket Bot applications, imported from `/home/bracketbot/bbapps` on bracketbot-0187.
The original Bracket Bot MIT license is retained in `LICENSE`.

The working checkout on the robot stays at `/home/bracketbot/bbapps`.
Apps use the existing `/home/bracketbot/bbos` installation and `uv`;
this repository does not include the robot OS or its environment.

- `teleop.py`, `quest_teleop/`: keyboard and headset teleoperation
- `examples/`: hardware inspection examples
- `nav/`, `inference/`: navigation and policy inference
- `stereo_capture_web.py`: stereo calibration capture
- `greeter/`, `mimic/`, `play_sound/`, `low_battery/`: robot apps

Run an app from this directory with `uv run <script.py>` after checking its
hardware requirements. Teleoperation and movement apps command real hardware.
Credentials belong in local environment variables or ignored `.env` files.
