"""Generate a private, bounded pair of demo credentials offline. Never deploy secrets."""
from __future__ import annotations

import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import time
import uuid
from urllib.parse import urlsplit


def validate_url(url: str) -> str:
    parsed = urlsplit(url)
    local = parsed.scheme == "ws" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (not parsed.hostname or not (parsed.scheme == "wss" or local)
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Use a wss:// LiveKit URL (ws:// is allowed only for localhost).")
    return url


def generate(*, url: str, api_key: str, api_secret: str, minutes: int = 30,
             driving: bool = False, preview: bool = False):
    from livekit import api

    validate_url(url)
    if driving and preview:
        raise ValueError("Choose either driving or preview, not both.")
    if not api_key or not api_secret:
        raise ValueError("Set LIVEKIT_API_KEY and LIVEKIT_API_SECRET privately.")
    if type(minutes) is not int or not 1 <= minutes <= 60:
        raise ValueError("Demo lifetime must be 1–60 minutes.")
    # A fresh room keeps credentials from previous demos out of this session.
    room = "realbot-demo-" + uuid.uuid4().hex[:16]
    robot, controller = "realbot-robot", "realbot-controller"
    expires = int(time.time()) + minutes * 60

    def token(identity, *, camera, commands):
        return (api.AccessToken(api_key, api_secret).with_identity(identity)
                .with_ttl(timedelta(minutes=minutes))
                .with_grants(api.VideoGrants(
                    room_join=True, room=room, can_publish=camera,
                    can_publish_sources=["camera"] if camera else [],
                    # Keep the receive connection available for data. The robot
                    # joins with auto_subscribe=False and has no media handlers.
                    can_subscribe=True, can_publish_data=commands,
                    can_update_own_metadata=False,
                )).to_jwt())

    robot_config = dict(version=1, kind="robot", url=url, roomId=room, robotIdentity=robot,
                        controllerIdentity=controller, expiresAt=expires,
                        mode="preview" if preview else "drive" if driving else "camera",
                        token=token(robot, camera=True, commands=True))
    browser_config = dict(version=1, kind="browser", url=url, roomId=room, robotIdentity=robot,
                          expiresAt=expires, mode=robot_config["mode"],
                          token=token(controller, camera=False, commands=driving or preview))
    return robot_config, browser_config


def write_configs(directory: Path, robot: dict, browser: dict):
    # Never overwrite existing credentials; fresh folder per issuance.
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, config in (("robot.session.json", robot), ("browser.session.json", browser)):
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, indent=2)
            stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="Private local .env; never a VITE_ file")
    parser.add_argument("--output", type=Path, required=True, help="New private output directory")
    parser.add_argument("--minutes", type=int, default=30)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--enable-driving", action="store_true", help="Issue controller data permission; default is camera-only")
    mode.add_argument("--preview-only", action="store_true", help="Issue a no-movement floor-preview session")
    args = parser.parse_args()
    if args.env_file:
        from dotenv import load_dotenv
        if not args.env_file.is_file():
            parser.error("Private environment file not found")
        load_dotenv(args.env_file, override=False)
    try:
        robot, browser = generate(url=os.environ.get("LIVEKIT_URL", ""),
                                  api_key=os.environ.get("LIVEKIT_API_KEY", ""),
                                  api_secret=os.environ.get("LIVEKIT_API_SECRET", ""),
                                  minutes=args.minutes, driving=args.enable_driving, preview=args.preview_only)
        write_configs(args.output, robot, browser)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"Created {robot['mode']} demo credentials in {args.output}. Keep both files private.")
    print("No network request was made. No robot services were started.")


if __name__ == "__main__":
    main()
