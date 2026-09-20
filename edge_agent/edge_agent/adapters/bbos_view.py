"""Read-only, depth-aligned floor selection source for the driving session."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import ctypes
import hashlib
import io
import math
from pathlib import Path
import time
import uuid

import numpy as np
from PIL import Image

from ..projection import FloorCapture, floor_goal


def timestamp(record) -> int:
    value = record["timestamp"]
    return int(value.astype("int64")) if hasattr(value, "astype") else int(value)


def small_jpeg(rgb: np.ndarray) -> bytes:
    image = Image.fromarray(rgb)
    image.thumbnail((320, 240))
    for quality in (80, 65, 45, 30, 20):
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality)
        if output.tell() <= 9_000:
            return output.getvalue()
    raise ValueError("Selection image too large. Try another view.")


class BbosFloorView:
    """Use within the existing remote_session, after disabling legacy teleop.

    Dependencies are installed in bbOS, not in a separate hardware-owning daemon.
    Calibration matches mapping's map_calib and camera_to_base_3x4 contracts.
    """

    def __enter__(self):
        from bbos import Config, Reader
        from bbos.daemons.nav import constants
        import yaml

        self.stack = ExitStack()
        try:
            depth_cfg, mapping_cfg = Config("depth"), Config("mapping")
            self.resolution = float(mapping_cfg.voxel_size_m)
            params = yaml.safe_load(Path(constants.__file__).with_name("params.yaml").read_text())
            self.clearance = max(.25, float(params["ROBOT_RADIUS_CELLS"]) * self.resolution)
            self.camera_to_base = np.asarray(depth_cfg.camera_to_base_3x4, dtype=float).reshape(3, 4)
            self.calibration = hashlib.sha256(Path(depth_cfg.calib_path).read_bytes()).hexdigest()
            library = ctypes.CDLL(str(mapping_cfg.library))
            library.map_calib.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int,
                                         np.ctypeslib.ndpointer(np.float32, flags="C"), ctypes.c_void_p]
            intrinsics = np.zeros(4, np.float32)
            if library.map_calib(str(depth_cfg.calib_path).encode(), int(depth_cfg.width_D), int(depth_cfg.height_D), intrinsics, None) != 0:
                raise ValueError("bbOS depth calibration unreadable")
            self.intrinsics = tuple(float(x) for x in intrinsics)
            self.depth = self.stack.enter_context(Reader("camera.depth", keeptime=False))
            self.rect = self.stack.enter_context(Reader("camera.rect", keeptime=False, aligned_to=self.depth))
            self.aligned_pose = self.stack.enter_context(Reader("slam.pose", keeptime=False, aligned_to=self.depth))
            self.readers = {name: self.stack.enter_context(Reader(name, keeptime=False))
                            for name in ("slam.pose", "slam.health", "nav.state", "mapping.grid2d")}
            self.last_capture = None
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        self.stack.__exit__(*args)

    @staticmethod
    def _read(reader, age_ms: int):
        reader.ready()
        record = reader.data if reader.readable else None
        if record is None or not 0 <= time.time_ns() - timestamp(record) <= age_ms * 1_000_000:
            raise ValueError("Robot sensor or navigation data is unavailable or stale.")
        return record

    def state(self):
        pose = self._read(self.readers["slam.pose"], 300)
        health = self._read(self.readers["slam.health"], 1_000)
        nav = self._read(self.readers["nav.state"], 1_000)
        grid = self._read(self.readers["mapping.grid2d"], 1_000)
        if not health["localized"] or any(health[k] for k in ("degraded", "stalled", "vo_lost")):
            raise ValueError("Localization is not ready.")
        xyh = self._pose(pose)
        revision = f'{self.calibration}:{int(pose["pgo_count"])}:{tuple(float(n) for n in grid["origin"])}'
        state = bytes(nav["state"]).split(b"\0", 1)[0].decode()
        return xyh, revision, grid, state

    @staticmethod
    def _pose(record):
        x, y = float(record["pos"][0]), float(record["pos"][1])
        yaw = 2 * math.atan2(float(record["quat"][2]), float(record["quat"][3]))
        if not all(math.isfinite(n) for n in (x, y, yaw)):
            raise ValueError("Invalid robot pose.")
        return (x, y, yaw)

    def ready(self) -> bool:
        try:
            self.state()
            return True
        except (ValueError, RuntimeError, KeyError):
            return False

    async def capture(self) -> FloorCapture:
        initial, revision, _, state = self.state()
        if state not in {"idle", "reached", "failed"}:
            raise ValueError("Wait for the robot to stop before choosing a view.")
        await asyncio.sleep(.35)
        current, new_revision, _, state = self.state()
        delta = math.atan2(math.sin(current[2]-initial[2]), math.cos(current[2]-initial[2]))
        if state not in {"idle", "reached", "failed"} or new_revision != revision or math.dist(current[:2], initial[:2]) > .01 or abs(delta) > math.radians(.5):
            raise ValueError("Robot is moving. Wait before choosing a view.")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            depth = self._read(self.depth, 750)
            # Do not advance the depth reference while waiting for its delayed SLAM pose.
            depth = depth.copy()
            while time.monotonic() < deadline:
                self.rect.ready()
                self.aligned_pose.ready()
                rect, pose = self.rect.data, self.aligned_pose.data
                if self.rect.readable and self.aligned_pose.readable and rect is not None and pose is not None:
                    if abs(timestamp(rect)-timestamp(depth)) <= 50_000_000 and abs(timestamp(pose)-timestamp(depth)) <= 100_000_000:
                        if time.time_ns() - timestamp(depth) > 750_000_000:
                            break
                        image = rect["left"].copy()
                        if image.shape[:2] != depth["depth"].shape:
                            raise ValueError("Rectified image and depth dimensions disagree.")
                        self.state()  # Recheck localization after awaiting sensors.
                        return FloorCapture(uuid.uuid4().hex, time.monotonic(), small_jpeg(image),
                                            depth["depth"].copy(), self.intrinsics,
                                            self.camera_to_base.copy(), self._pose(pose), revision)
                await asyncio.sleep(.02)
        raise ValueError("Could not match camera, depth and pose. Try again.")

    def resolve(self, capture: FloorCapture, u: float, v: float):
        pose, revision, grid, state = self.state()
        if state not in {"idle", "reached", "failed"}:
            raise ValueError("Another navigation operation is active.")
        return floor_goal(capture, u, v, current_pose=pose, revision=revision,
                          grid=grid["grid"], origin=tuple(grid["origin"]),
                          resolution=self.resolution, clearance_m=self.clearance)
