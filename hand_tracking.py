# /// script
# requires-python = ">=3.10,<3.12"
# dependencies = [
#   "bbos",
#   "fastapi>=0.115,<1",
#   "mediapipe==1.0.1",
#   "python-dotenv>=1,<2",
#   "uvicorn>=0.34,<1",
#   "wsproto>=1.2,<2",
# ]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""Live MediaPipe hand tracking from BracketBot's stereo head cameras.

The camera daemon publishes both head cameras as one side-by-side JPEG on
``camera.head.jpeg``. This app splits that frame, gives each eye its own
MediaPipe video tracker, and exposes annotated video plus all 21 landmarks.

Run on the robot with::

    uv run hand_tracking.py --port 8006

Then open ``http://bracketbot-0187.local:8006/``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import cv2
import mediapipe as mp
import numpy as np
import uvicorn
from bbos import Config, Reader
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from action_landmarking import ActionLandmarkRecorder, record_timestamp, slam_to_camera
from visitor_drive import router as visitor_drive_router


MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/"
    "gesture_recognizer/float16/1/gesture_recognizer.task"
)
MODEL_SHA256 = "97952348cf6a6a4915c2ea1496b4b37ebabc50cbbf80571435643c455f2b0482"
DEFAULT_MODEL = Path.home() / ".cache" / "realbot" / "gesture_recognizer.task"
THUMBS_UP_MIN_SCORE = 0.60
CAMERA_TOPIC = "camera.head.jpeg"

LANDMARK_NAMES = (
    "wrist",
    "thumb_cmc", "thumb_mcp", "thumb_ip", "thumb_tip",
    "index_mcp", "index_pip", "index_dip", "index_tip",
    "middle_mcp", "middle_pip", "middle_dip", "middle_tip",
    "ring_mcp", "ring_pip", "ring_dip", "ring_tip",
    "pinky_mcp", "pinky_pip", "pinky_dip", "pinky_tip",
)

FINGER_LANDMARKS = {
    "thumb": (1, 2, 3, 4),
    "index": (5, 6, 7, 8),
    "middle": (9, 10, 11, 12),
    "ring": (13, 14, 15, 16),
    "pinky": (17, 18, 19, 20),
}

CONNECTIONS = (
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20), (0, 17),
)


def ensure_model(path: Path) -> Path:
    """Download the pinned official model once and verify its digest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and _sha256(path) == MODEL_SHA256:
        return path

    partial = path.with_suffix(path.suffix + ".part")
    print(f"[model] downloading {MODEL_URL}", flush=True)
    urllib.request.urlretrieve(MODEL_URL, partial)
    digest = _sha256(partial)
    if digest != MODEL_SHA256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"hand model checksum mismatch: {digest}")
    partial.replace(path)
    print(f"[model] ready: {path}", flush=True)
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _recognizer(model_path: Path, max_hands: int) -> Any:
    options = mp.tasks.vision.GestureRecognizerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path)),
        running_mode=mp.tasks.vision.RunningMode.VIDEO,
        num_hands=max_hands,
        min_hand_detection_confidence=0.45,
        min_hand_presence_confidence=0.45,
        min_tracking_confidence=0.45,
    )
    return mp.tasks.vision.GestureRecognizer.create_from_options(options)


def _point(lm: Any, width: int, height: int, index: int) -> dict[str, Any]:
    x = float(lm.x)
    y = float(lm.y)
    z = float(lm.z)
    return {
        "index": index,
        "name": LANDMARK_NAMES[index],
        "x": x,
        "y": y,
        "z": z,
        "pixel_x": round(x * width, 1),
        "pixel_y": round(y * height, 1),
    }


def _world_point(lm: Any, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "name": LANDMARK_NAMES[index],
        "x_m": float(lm.x),
        "y_m": float(lm.y),
        "z_m": float(lm.z),
    }


def serialize_result(result: Any, width: int, height: int) -> list[dict[str, Any]]:
    hands: list[dict[str, Any]] = []
    for hand_index, landmarks in enumerate(result.hand_landmarks):
        classifications = (
            result.handedness[hand_index]
            if hand_index < len(result.handedness)
            else []
        )
        classification = classifications[0] if classifications else None
        model_handedness = (
            classification.category_name if classification else "Unknown"
        )
        # MediaPipe's handedness classifier assumes selfie-mirrored input. The
        # robot publishes unmirrored camera images, so report anatomical labels.
        handedness = {"Left": "Right", "Right": "Left"}.get(
            model_handedness, model_handedness
        )
        world = (
            result.hand_world_landmarks[hand_index]
            if hand_index < len(result.hand_world_landmarks)
            else []
        )
        gesture_categories = (
            result.gestures[hand_index]
            if hand_index < len(result.gestures)
            else []
        )
        gesture_category = gesture_categories[0] if gesture_categories else None
        gesture_model_name = (
            gesture_category.category_name if gesture_category else "None"
        )
        gesture_score = float(gesture_category.score) if gesture_category else 0.0
        thumbs_up = (
            gesture_model_name == "Thumb_Up"
            and gesture_score >= THUMBS_UP_MIN_SCORE
        )
        points = [_point(lm, width, height, i) for i, lm in enumerate(landmarks)]
        hands.append(
            {
                "handedness": handedness,
                "model_handedness": model_handedness,
                "handedness_score": (
                    round(float(classification.score), 4) if classification else 0.0
                ),
                "gesture": "thumbs_up" if thumbs_up else "none",
                "gesture_model_name": gesture_model_name,
                "gesture_score": round(gesture_score, 4),
                "thumbs_up": thumbs_up,
                "landmarks": points,
                "world_landmarks": [
                    _world_point(lm, i) for i, lm in enumerate(world)
                ],
                "fingers": {
                    name: [points[i] for i in indices]
                    for name, indices in FINGER_LANDMARKS.items()
                },
            }
        )
    return hands


def draw_hands(image: np.ndarray, hands: list[dict[str, Any]]) -> None:
    colors = ((40, 230, 255), (255, 90, 220), (80, 255, 120), (255, 180, 60))
    for hand_index, hand in enumerate(hands):
        color = colors[hand_index % len(colors)]
        points = hand["landmarks"]
        pixels = [
            (int(round(point["pixel_x"])), int(round(point["pixel_y"])))
            for point in points
        ]
        for start, end in CONNECTIONS:
            cv2.line(image, pixels[start], pixels[end], color, 2, cv2.LINE_AA)
        for index, pixel in enumerate(pixels):
            radius = 5 if index in (4, 8, 12, 16, 20) else 3
            cv2.circle(image, pixel, radius, (20, 20, 20), -1, cv2.LINE_AA)
            cv2.circle(image, pixel, max(2, radius - 2), color, -1, cv2.LINE_AA)
        wrist_x, wrist_y = pixels[0]
        label = f"{hand['handedness']} {hand['handedness_score']:.2f}"
        if hand["thumbs_up"]:
            label += f"  THUMBS UP {hand['gesture_score']:.2f}"
        cv2.putText(
            image,
            label,
            (max(4, wrist_x - 25), max(22, wrist_y - 12)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            2,
            cv2.LINE_AA,
        )

class StereoCalibration:
    """Metric fisheye stereo model supplied by the robot's depth daemon."""

    def __init__(self) -> None:
        depth = Config("depth")
        head = Config("cam_head")
        (
            self.camera_matrix_1,
            self.distortion_1,
            self.camera_matrix_2,
            self.distortion_2,
            self.rectification_1,
            self.rectification_2,
            self.projection_1,
            projection_2_mm,
            _q,
            baseline_m,
            _fx,
            _rotation,
            _translation,
        ) = depth.camera_cal()
        self.camera_matrix_1 = np.asarray(self.camera_matrix_1, np.float64)
        self.distortion_1 = np.asarray(self.distortion_1, np.float64).reshape(4, 1)
        self.camera_matrix_2 = np.asarray(self.camera_matrix_2, np.float64)
        self.distortion_2 = np.asarray(self.distortion_2, np.float64).reshape(4, 1)
        self.rectification_1 = np.asarray(self.rectification_1, np.float64)
        self.rectification_2 = np.asarray(self.rectification_2, np.float64)
        self.projection_1 = np.asarray(self.projection_1, np.float64)
        self.projection_2 = np.asarray(projection_2_mm, np.float64).copy()
        # The calibration file stores translation in millimetres. Scaling the
        # projection's fourth column makes cv2.triangulatePoints return metres.
        self.projection_2[:, 3] /= 1000.0
        self.baseline_m = float(baseline_m)
        self.eye_size = (int(head.width // 2), int(head.height))
        self.path = str(depth.calib_path)

    def project_point(self, point: np.ndarray, eye: int) -> np.ndarray | None:
        matrix, distortion, rotation, projection = (
            (self.camera_matrix_1, self.distortion_1, self.rectification_1, self.projection_1)
            if eye == 0 else
            (self.camera_matrix_2, self.distortion_2, self.rectification_2, self.projection_2)
        )
        raw = rotation.T @ (point + np.linalg.solve(projection[:, :3], projection[:, 3]))
        if not np.isfinite(raw).all() or raw[2] <= 0:
            return None
        pixel = cv2.fisheye.projectPoints(
            raw.reshape(1, 1, 3), np.zeros(3), np.zeros(3), matrix, distortion,
        )[0].reshape(2)
        return pixel if np.isfinite(pixel).all() else None

    def summary(self) -> dict[str, Any]:
        return {
            "calibration_path": self.path,
            "baseline_m": round(self.baseline_m, 9),
            "baseline_mm": round(self.baseline_m * 1000.0, 4),
            "calibrated_eye_size": list(self.eye_size),
            "coordinate_frame": {
                "origin": "midpoint between the rectified camera optical centres",
                "x": "right",
                "y": "down",
                "z": "forward",
                "units": "metres",
            },
        }

    def rectify_hand(
        self,
        hand: dict[str, Any],
        camera_index: int,
        source_width: int,
        source_height: int,
    ) -> np.ndarray:
        if (source_width, source_height) != self.eye_size:
            raise ValueError(
                f"stereo calibration is for {self.eye_size[0]}x{self.eye_size[1]} "
                f"per eye, received {source_width}x{source_height}"
            )
        raw = np.asarray(
            [
                [point["x"] * source_width, point["y"] * source_height]
                for point in hand["landmarks"]
            ],
            dtype=np.float64,
        ).reshape(-1, 1, 2)
        if camera_index == 0:
            matrix, distortion = self.camera_matrix_1, self.distortion_1
            rotation, projection = self.rectification_1, self.projection_1
        else:
            matrix, distortion = self.camera_matrix_2, self.distortion_2
            rotation, projection = self.rectification_2, self.projection_2
        return cv2.fisheye.undistortPoints(
            raw,
            matrix,
            distortion,
            R=rotation,
            P=projection[:, :3],
        ).reshape(-1, 2)

    def triangulate(
        self,
        camera_1_hands: list[dict[str, Any]],
        camera_2_hands: list[dict[str, Any]],
        source_width: int,
        source_height: int,
    ) -> dict[str, Any]:
        summary = self.summary()
        summary["matches"] = []
        summary["match_count"] = 0
        summary["thumbs_up_count"] = 0
        if not camera_1_hands or not camera_2_hands:
            return summary

        rectified_1 = [
            self.rectify_hand(hand, 0, source_width, source_height)
            for hand in camera_1_hands
        ]
        rectified_2 = [
            self.rectify_hand(hand, 1, source_width, source_height)
            for hand in camera_2_hands
        ]

        candidates: list[tuple[float, int, int, float, float, float]] = []
        for left_index, (left_hand, left_points) in enumerate(
            zip(camera_1_hands, rectified_1)
        ):
            for right_index, (right_hand, right_points) in enumerate(
                zip(camera_2_hands, rectified_2)
            ):
                epipolar_error = float(
                    np.median(np.abs(left_points[:, 1] - right_points[:, 1]))
                )
                point_delta = left_points - right_points
                disparity = float(np.median(point_delta[:, 0]))
                median_delta = np.median(point_delta, axis=0)
                shape_error = float(
                    np.median(np.linalg.norm(point_delta - median_delta, axis=1))
                )
                if disparity <= 0.5 or epipolar_error > 30.0 or shape_error > 35.0:
                    continue
                labels_match = left_hand["handedness"] == right_hand["handedness"]
                label_confidence = min(
                    left_hand["handedness_score"], right_hand["handedness_score"]
                )
                handedness_penalty = 0.0
                if not labels_match:
                    handedness_penalty = 80.0 if label_confidence >= 0.75 else 20.0
                candidates.append(
                    (
                        epipolar_error + 2.0 * shape_error + handedness_penalty,
                        left_index,
                        right_index,
                        epipolar_error,
                        disparity,
                        shape_error,
                    )
                )

        used_1: set[int] = set()
        used_2: set[int] = set()
        for (
            _cost,
            left_index,
            right_index,
            epipolar_error,
            disparity,
            shape_error,
        ) in sorted(candidates):
            if left_index in used_1 or right_index in used_2:
                continue
            match = self._triangulate_match(
                camera_1_hands[left_index],
                camera_2_hands[right_index],
                rectified_1[left_index],
                rectified_2[right_index],
                left_index,
                right_index,
                epipolar_error,
                disparity,
                shape_error,
            )
            # A false hand association usually produces very few geometrically
            # valid landmarks. Do not reserve either hand for such a match.
            if match["valid_landmark_count"] < 8:
                continue
            used_1.add(left_index)
            used_2.add(right_index)
            summary["matches"].append(match)

        summary["match_count"] = len(summary["matches"])
        summary["thumbs_up_count"] = sum(
            bool(match["thumbs_up"]) for match in summary["matches"]
        )
        return summary

    def _triangulate_match(
        self,
        hand_1: dict[str, Any],
        hand_2: dict[str, Any],
        points_1: np.ndarray,
        points_2: np.ndarray,
        index_1: int,
        index_2: int,
        epipolar_error: float,
        disparity: float,
        shape_error: float,
    ) -> dict[str, Any]:
        homogeneous = cv2.triangulatePoints(
            self.projection_1,
            self.projection_2,
            points_1.T,
            points_2.T,
        )
        divisor = homogeneous[3]
        xyz = np.full((len(LANDMARK_NAMES), 3), np.nan, dtype=np.float64)
        safe = np.abs(divisor) > 1e-9
        xyz[safe] = (homogeneous[:3, safe] / divisor[safe]).T

        xyz_h = np.column_stack((xyz, np.ones(len(xyz))))
        reprojection_1 = (self.projection_1 @ xyz_h.T).T
        reprojection_2 = (self.projection_2 @ xyz_h.T).T
        projected_1 = reprojection_1[:, :2] / reprojection_1[:, 2:3]
        projected_2 = reprojection_2[:, :2] / reprojection_2[:, 2:3]
        errors = 0.5 * (
            np.linalg.norm(projected_1 - points_1, axis=1)
            + np.linalg.norm(projected_2 - points_2, axis=1)
        )
        point_epipolar_errors = np.abs(points_1[:, 1] - points_2[:, 1])
        point_disparities = points_1[:, 0] - points_2[:, 0]
        valid = (
            np.isfinite(xyz).all(axis=1)
            & np.isfinite(errors)
            & (xyz[:, 2] >= 0.08)
            & (xyz[:, 2] <= 8.0)
            & (errors <= 20.0)
            & (point_epipolar_errors <= 20.0)
            & (point_disparities > 0.5)
        )

        landmarks: list[dict[str, Any]] = []
        for landmark_index, point in enumerate(xyz):
            item: dict[str, Any] = {
                "index": landmark_index,
                "name": LANDMARK_NAMES[landmark_index],
                "valid": bool(valid[landmark_index]),
                "reprojection_error_px": (
                    round(float(errors[landmark_index]), 3)
                    if np.isfinite(errors[landmark_index])
                    else None
                ),
                "epipolar_error_px": round(
                    float(point_epipolar_errors[landmark_index]), 3
                ),
                "disparity_px": round(float(point_disparities[landmark_index]), 3),
            }
            if valid[landmark_index]:
                item["position_left_camera_m"] = _vector(point)
                item["position_camera_pair_m"] = _vector(
                    point - np.array([self.baseline_m / 2.0, 0.0, 0.0])
                )
            else:
                item["position_left_camera_m"] = None
                item["position_camera_pair_m"] = None
            landmarks.append(item)

        # Finger-base MCP joints are the most stable semantic correspondences.
        # The wrist is often partly occluded by a sleeve and biased differently
        # in each eye, so it is still triangulated but excluded from hand centre.
        palm_indices = np.asarray((5, 9, 13, 17), dtype=np.int32)
        valid_palm = palm_indices[valid[palm_indices]]
        position_indices = valid_palm if len(valid_palm) >= 3 else np.flatnonzero(valid)
        position_left = (
            np.median(xyz[position_indices], axis=0)
            if len(position_indices)
            else None
        )
        valid_errors = errors[valid]
        valid_depth = xyz[valid, 2]
        median_reprojection = (
            float(np.median(valid_errors)) if len(valid_errors) else float("inf")
        )
        depth_spread = (
            float(np.median(np.abs(valid_depth - np.median(valid_depth))))
            if len(valid_depth)
            else float("inf")
        )
        valid_count = int(np.count_nonzero(valid))
        quality = "poor"
        if (
            valid_count >= 18
            and epipolar_error <= 5.0
            and shape_error <= 10.0
            and depth_spread <= 0.20
        ):
            quality = "good"
        elif (
            valid_count >= 12
            and epipolar_error <= 12.0
            and shape_error <= 20.0
            and depth_spread <= 0.35
        ):
            quality = "okay"

        match: dict[str, Any] = {
            "camera_1_hand_index": index_1,
            "camera_2_hand_index": index_2,
            "handedness": (
                hand_1["handedness"]
                if hand_1["handedness"] == hand_2["handedness"]
                else "uncertain"
            ),
            "quality": quality,
            "valid_landmark_count": valid_count,
            "epipolar_error_px": round(epipolar_error, 3),
            "shape_error_px": round(shape_error, 3),
            "median_disparity_px": round(disparity, 3),
            "median_reprojection_error_px": (
                round(median_reprojection, 3)
                if np.isfinite(median_reprojection)
                else None
            ),
            "depth_spread_m": (
                round(depth_spread, 4) if np.isfinite(depth_spread) else None
            ),
            # Be conservative for the stereo result: both independent camera
            # recognizers must agree that the matched hand is a thumbs-up.
            "gesture": (
                "thumbs_up"
                if hand_1["thumbs_up"] and hand_2["thumbs_up"]
                else "none"
            ),
            "gesture_score": (
                round(min(hand_1["gesture_score"], hand_2["gesture_score"]), 4)
                if hand_1["thumbs_up"] and hand_2["thumbs_up"]
                else 0.0
            ),
            "thumbs_up": bool(hand_1["thumbs_up"] and hand_2["thumbs_up"]),
            "camera_gestures": [
                {
                    "name": hand_1["gesture_model_name"],
                    "score": hand_1["gesture_score"],
                },
                {
                    "name": hand_2["gesture_model_name"],
                    "score": hand_2["gesture_score"],
                },
            ],
            "landmarks": landmarks,
        }
        if position_left is None:
            match.update(
                {
                    "position_left_camera_m": None,
                    "position_camera_pair_m": None,
                    "distance_to_camera_1_m": None,
                    "distance_to_camera_2_m": None,
                    "distance_to_camera_pair_m": None,
                }
            )
        else:
            right_camera = np.array([self.baseline_m, 0.0, 0.0])
            pair_position = position_left - right_camera / 2.0
            match.update(
                {
                    "position_left_camera_m": _vector(position_left),
                    "position_camera_pair_m": _vector(pair_position),
                    "distance_to_camera_1_m": round(float(np.linalg.norm(position_left)), 4),
                    "distance_to_camera_2_m": round(
                        float(np.linalg.norm(position_left - right_camera)), 4
                    ),
                    "distance_to_camera_pair_m": round(
                        float(np.linalg.norm(pair_position)), 4
                    ),
                }
            )
        return match


def _vector(point: np.ndarray) -> dict[str, float]:
    return {
        "x": round(float(point[0]), 5),
        "y": round(float(point[1]), 5),
        "z": round(float(point[2]), 5),
    }


def draw_stereo_positions(
    images: list[np.ndarray],
    camera_payloads: list[dict[str, Any]],
    stereo: dict[str, Any],
) -> None:
    for match in stereo["matches"]:
        position = match.get("position_camera_pair_m")
        if not position:
            continue
        label = f"3D Z={position['z']:.2f}m  {match['quality']}"
        if match["thumbs_up"]:
            label += f"  THUMBS UP {match['gesture_score']:.2f}"
        for camera_index, hand_key in enumerate(
            ("camera_1_hand_index", "camera_2_hand_index")
        ):
            hand_index = match[hand_key]
            hand = camera_payloads[camera_index]["hands"][hand_index]
            wrist = hand["landmarks"][0]
            x = max(4, int(round(wrist["pixel_x"])) - 25)
            y = min(images[camera_index].shape[0] - 8, int(round(wrist["pixel_y"])) + 22)
            cv2.putText(
                images[camera_index],
                label,
                (x, y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.48,
                (80, 255, 120) if match["quality"] != "poor" else (60, 160, 255),
                2,
                cv2.LINE_AA,
            )


class StereoHandTracker:
    def __init__(
        self,
        model_path: Path,
        inference_width: int = 640,
        max_hands: int = 2,
        jpeg_quality: int = 80,
    ) -> None:
        self.model_path = model_path
        self.inference_width = inference_width
        self.max_hands = max_hands
        self.jpeg_quality = jpeg_quality
        self.calibration = StereoCalibration()
        depth = Config("depth")
        calibration_revision = hashlib.sha256(Path(depth.calib_path).read_bytes()).hexdigest()
        self.action_landmarks = ActionLandmarkRecorder(
            projection_1=self.calibration.projection_1,
            calibrated_size=self.calibration.eye_size,
            base_from_camera=depth.T_base_cam.mat(),
            calibration_revision=calibration_revision,
            store_path=Path(
                os.getenv(
                    "REALBOT_ACTION_LANDMARKS",
                    "~/.local/share/realbot/action_landmarks.json",
                )
            ),
            wav_directory=Path(__file__).with_name("action_landmarking_wavs"),
        )
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest_jpeg = b""
        self._raw_jpeg = b""
        self._tracking_until = 0.0
        self._state: dict[str, Any] = {
            "status": "starting",
            "source": CAMERA_TOPIC,
            "mediapipe_version": mp.__version__,
            "gesture_model": "MediaPipe canned gestures",
            "thumbs_up_min_score": THUMBS_UP_MIN_SCORE,
            "camera_1": {"hands": []},
            "camera_2": {"hands": []},
            "stereo": self.calibration.summary()
            | {"match_count": 0, "thumbs_up_count": 0, "matches": []},
            "action_landmarking": self.action_landmarks.snapshot(),
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.action_landmarks.start()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.action_landmarks.close()

    def enable_tracking(self) -> None:
        self._tracking_until = time.monotonic() + 3.0

    def snapshot(self, annotated: bool = True) -> tuple[bytes, dict[str, Any]]:
        with self._lock:
            # JSON round-trip makes a safe copy of nested landmark lists.
            return (self._latest_jpeg if annotated else self._raw_jpeg), json.loads(json.dumps(self._state))

    def _publish(self, jpeg: bytes, state: dict[str, Any], raw: bytes) -> None:
        with self._lock:
            self._latest_jpeg = jpeg
            self._raw_jpeg = raw
            self._state = state

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._state = {**self._state, "status": "error", "error": message}

    def _process_eye(
        self,
        source: np.ndarray,
        landmarker: Any,
        timestamp_ms: int,
        inference_height: int,
        tracking: bool = True,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        started = time.perf_counter()
        resized = cv2.resize(
            source,
            (self.inference_width, inference_height),
            interpolation=cv2.INTER_AREA,
        )
        if not tracking:
            return resized, {"hands": [], "hand_count": 0, "inference_ms": 0.0}
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        media_image = mp.Image(
            image_format=mp.ImageFormat.SRGB,
            data=np.ascontiguousarray(rgb),
        )
        result = landmarker.recognize_for_video(media_image, timestamp_ms)
        hands = serialize_result(result, self.inference_width, inference_height)
        draw_hands(resized, hands)
        return resized, {
            "hands": hands,
            "hand_count": len(hands),
            "inference_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    def _run(self) -> None:
        frame_count = 0
        fps = 0.0
        previous_frame_time: float | None = None
        last_timestamp_ms = 0
        try:
            with ExitStack() as stack:
                trackers = [
                    stack.enter_context(_recognizer(self.model_path, self.max_hands)),
                    stack.enter_context(_recognizer(self.model_path, self.max_hands)),
                ]
                executor = stack.enter_context(
                    ThreadPoolExecutor(max_workers=2, thread_name_prefix="hand-eye")
                )
                depth_reader = stack.enter_context(
                    Reader("camera.depth", keeptime=False)
                )
                reader = stack.enter_context(
                    Reader(CAMERA_TOPIC, keeptime=False, aligned_to=depth_reader)
                )
                pose_reader = stack.enter_context(
                    Reader("slam.pose", keeptime=False, aligned_to=reader)
                )
                health_reader = stack.enter_context(Reader("slam.health", keeptime=False))
                imu_reader = stack.enter_context(
                    Reader("imu.orientation", keeptime=False, aligned_to=reader)
                )
                pitch_rad = 0.0

                while not self._stop.is_set():
                    if not depth_reader.ready() or not reader.ready():
                        time.sleep(0.001)
                        continue

                    started = time.perf_counter()
                    depth_record = depth_reader.data.copy()
                    source_timestamp_ns = record_timestamp(reader.data)
                    jpeg_len = int(reader.data["jpeg_len"])
                    raw = bytes(reader.data["jpeg"][:jpeg_len])
                    stereo = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
                    if stereo is None or stereo.shape[1] % 2:
                        self._set_error("camera.head.jpeg did not decode as an even stereo frame")
                        time.sleep(0.05)
                        continue

                    source_height, stereo_width = stereo.shape[:2]
                    source_eye_width = stereo_width // 2
                    source_eyes = (
                        stereo[:, :source_eye_width],
                        stereo[:, source_eye_width:],
                    )
                    scale = self.inference_width / source_eye_width
                    inference_height = max(1, round(source_height * scale))
                    timestamp_ms = time.monotonic_ns() // 1_000_000
                    if timestamp_ms <= last_timestamp_ms:
                        timestamp_ms = last_timestamp_ms + 1
                    last_timestamp_ms = timestamp_ms
                    tracking = time.monotonic() < self._tracking_until
                    if not tracking:
                        self.action_landmarks.pause()

                    jobs = [
                        executor.submit(
                            self._process_eye,
                            source,
                            landmarker,
                            timestamp_ms,
                            inference_height,
                            tracking,
                        )
                        for source, landmarker in zip(source_eyes, trackers)
                    ]
                    processed = [job.result() for job in jobs]
                    output_images = [item[0] for item in processed]
                    camera_payloads = [item[1] for item in processed]

                    stereo_result = self.calibration.triangulate(
                        camera_payloads[0]["hands"],
                        camera_payloads[1]["hands"],
                        source_eye_width,
                        source_height,
                    )
                    draw_stereo_positions(output_images, camera_payloads, stereo_result)

                    pose_reader.ready()
                    health_reader.ready()
                    if imu_reader.ready():
                        pitch_rad = math.radians(float(imu_reader.data["rpy"][1]))
                    pose_record = (
                        pose_reader.data.copy()
                        if pose_reader.readable and pose_reader.data is not None
                        else None
                    )
                    health_record = (
                        health_reader.data.copy()
                        if health_reader.readable and health_reader.data is not None
                        else None
                    )
                    if tracking:
                        self.action_landmarks.observe(
                            stereo=stereo_result,
                            rgb_timestamp_ns=source_timestamp_ns,
                            depth_record=depth_record,
                            pose_record=pose_record,
                            health_record=health_record,
                            pitch_rad=pitch_rad,
                        )
                    action_state = self.action_landmarks.snapshot()

                    visible_actions = []
                    pose_ts = record_timestamp(pose_record)
                    if (pose_ts is not None and source_timestamp_ns is not None
                            and abs(pose_ts-source_timestamp_ns) <= 100_000_000
                            and self.action_landmarks._slam_healthy(health_record)):
                        for item in self.action_landmarks.records():
                            if item.get("calibration_revision") != self.action_landmarks.calibration_revision:
                                continue
                            point = slam_to_camera(
                                np.array([item["point_slam_m"][axis] for axis in "xyz"]),
                                self.action_landmarks.base_from_camera, pose_record, pitch_rad,
                            )
                            label = item["action"]["display_name"]
                            color = item["action"]["marker_color"].lstrip("#")
                            bgr = tuple(int(color[i:i+2], 16) for i in (4, 2, 0))
                            for eye, output in enumerate(output_images):
                                pixel = self.calibration.project_point(point, eye)
                                if pixel is None:
                                    continue
                                x, y = np.rint(pixel * scale).astype(int)
                                h, w = output.shape[:2]
                                if not (0 <= x < w and 0 <= y < h):
                                    continue
                                if eye == 0:
                                    visible_actions.append({"id": item["id"], "action_id": item["action_id"],
                                                            "label": label, "x": x / w, "y": y / h})
                                cv2.circle(output, (x, y), 8, (0, 0, 0), -1, cv2.LINE_AA)
                                cv2.circle(output, (x, y), 5, bgr, -1, cv2.LINE_AA)
                                text_width = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, .5, 1)[0][0]
                                at = (max(0, min(x+12, w-text_width-2)), max(16, y-12))
                                for thickness, ink in ((3, (0, 0, 0)), (1, bgr)):
                                    cv2.putText(output, label, at, cv2.FONT_HERSHEY_SIMPLEX,
                                                .5, ink, thickness, cv2.LINE_AA)

                    combined = np.hstack(output_images)
                    if action_state["state"] != "idle":
                        cv2.rectangle(combined, (0, 34), (combined.shape[1], 66), (0, 0, 0), -1)
                        cv2.putText(
                            combined,
                            f"ACTION {action_state['state'].upper()}: {action_state['message']}",
                            (10, 57),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.55,
                            (80, 255, 120) if action_state["state"] == "recording" else (220, 220, 220),
                            2,
                            cv2.LINE_AA,
                        )
                    ok, encoded = cv2.imencode(
                        ".jpg",
                        combined,
                        [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality],
                    )
                    if not ok:
                        self._set_error("could not encode annotated preview")
                        continue

                    now = time.perf_counter()
                    if previous_frame_time is not None:
                        instantaneous = 1.0 / max(now - previous_frame_time, 1e-6)
                        fps = instantaneous if fps == 0.0 else 0.9 * fps + 0.1 * instantaneous
                    previous_frame_time = now
                    frame_count += 1
                    state = {
                        "status": "running",
                        "source": CAMERA_TOPIC,
                        "mediapipe_version": mp.__version__,
                        "tracking_active": tracking,
                        "gesture_model": "MediaPipe canned gestures",
                        "thumbs_up_min_score": THUMBS_UP_MIN_SCORE,
                        "timestamp_ms": timestamp_ms,
                        "source_timestamp_ns": source_timestamp_ns,
                        "frame_count": frame_count,
                        "fps": round(fps, 2),
                        "inference_ms": round((now - started) * 1000, 1),
                        "source_stereo_size": [stereo_width, source_height],
                        "source_eye_size": [source_eye_width, source_height],
                        "inference_eye_size": [self.inference_width, inference_height],
                        "landmarks_per_hand": len(LANDMARK_NAMES),
                        "handedness": "anatomical; corrected for unmirrored cameras",
                        "camera_1": camera_payloads[0],
                        "camera_2": camera_payloads[1],
                        "stereo": stereo_result,
                        "action_landmarking": action_state,
                        "visible_actions": visible_actions,
                    }
                    ok, raw_preview = cv2.imencode(".jpg", cv2.resize(stereo,
                        (self.inference_width * 2, inference_height)),
                        [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
                    if ok:
                        self._publish(encoded.tobytes(), state, raw_preview.tobytes())
                    self._stop.wait(max(0, .1 - (time.perf_counter() - started)))
        except Exception as exc:  # keep the health endpoint useful on failures
            self._set_error(f"{type(exc).__name__}: {exc}")
            print(f"[tracker] fatal: {type(exc).__name__}: {exc}", flush=True)


app = FastAPI(title="BracketBot stereo hand tracking")
app.include_router(visitor_drive_router)
tracker: StereoHandTracker | None = None


def _tracker() -> StereoHandTracker:
    if tracker is None:
        raise RuntimeError("tracker has not started")
    return tracker


@app.get("/health")
def health() -> dict[str, Any]:
    _, state = _tracker().snapshot()
    stereo = state.get("stereo", {})
    return {
        key: value
        for key, value in state.items()
        if key not in ("camera_1", "camera_2", "stereo")
    } | {
        "camera_1_hands": state.get("camera_1", {}).get("hand_count", 0),
        "camera_2_hands": state.get("camera_2", {}).get("hand_count", 0),
        "stereo_match_count": stereo.get("match_count", 0),
        "baseline_m": stereo.get("baseline_m"),
        "hands_3d": [
            {
                key: match.get(key)
                for key in (
                    "handedness",
                    "quality",
                    "valid_landmark_count",
                    "gesture",
                    "gesture_score",
                    "thumbs_up",
                    "position_camera_pair_m",
                    "distance_to_camera_pair_m",
                )
            }
            for match in stereo.get("matches", [])
        ],
    }


@app.get("/landmarks")
def landmarks() -> dict[str, Any]:
    _, state = _tracker().snapshot()
    return state


@app.get("/stereo")
def stereo_positions() -> dict[str, Any]:
    _, state = _tracker().snapshot()
    return state.get("stereo", {})


@app.get("/gestures")
def gestures() -> dict[str, Any]:
    _, state = _tracker().snapshot()

    def camera_gestures(camera: str) -> list[dict[str, Any]]:
        return [
            {
                "hand_index": index,
                "handedness": hand["handedness"],
                "gesture": hand["gesture"],
                "model_name": hand["gesture_model_name"],
                "score": hand["gesture_score"],
                "thumbs_up": hand["thumbs_up"],
            }
            for index, hand in enumerate(state.get(camera, {}).get("hands", []))
        ]

    stereo = state.get("stereo", {})
    return {
        "timestamp_ms": state.get("timestamp_ms"),
        "camera_1": camera_gestures("camera_1"),
        "camera_2": camera_gestures("camera_2"),
        "stereo": [
            {
                key: match.get(key)
                for key in (
                    "camera_1_hand_index",
                    "camera_2_hand_index",
                    "handedness",
                    "gesture",
                    "gesture_score",
                    "thumbs_up",
                    "camera_gestures",
                )
            }
            for match in stereo.get("matches", [])
        ],
        "thumbs_up_count": stereo.get("thumbs_up_count", 0),
    }


@app.get("/actions")
def action_landmarks() -> dict[str, Any]:
    service = _tracker().action_landmarks
    return {"recording": service.snapshot(), "landmarks": service.records()}


@app.get("/frame.jpg")
def frame(annotated: bool = False) -> Response:
    jpeg, _ = _tracker().snapshot(annotated)
    if not jpeg:
        return Response(status_code=503)
    return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/stream")
async def stream(annotated: bool = False) -> StreamingResponse:
    async def frames():
        previous = b""
        while True:
            if annotated:
                _tracker().enable_tracking()
            jpeg, _ = _tracker().snapshot(annotated)
            if jpeg and jpeg != previous:
                previous = jpeg
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\n"
                    b"Cache-Control: no-store\r\n\r\n" + jpeg + b"\r\n"
                )
            else:
                await asyncio.sleep(0.01)

    return StreamingResponse(
        frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.post("/tracking")
def tracking() -> dict[str, bool]:
    _tracker().enable_tracking()
    return {"active": True}


@app.get("/slam-map")
def slam_map():
    reader = Reader("mapping.voxels", keeptime=False)
    try:
        if not reader.ready():
            raise HTTPException(503, "Waiting for the SLAM map")
        data = reader.data
        timestamp_ns = int(data['timestamp'].astype('int64'))
        if not 0 <= time.time_ns() - timestamp_ns < 5_000_000_000:
            raise HTTPException(503, "SLAM map is not updating")
        count = int(data['num_voxels'])
        if count == 0:
            raise HTTPException(503, "SLAM map is empty")
        # Bound the live preview payload; the robot retains the full map.
        step = max(1, math.ceil(count / 40_000))
        points = data['coords'][:count:step].copy()
        points = points[:, [0, 2, 1]]
        points[:, 2] *= -1  # SLAM z-up to Three.js y-up.
        return {
            'positions': points.round(4).reshape(-1).tolist(),
            'colors': data['colors'][:count:step].reshape(-1).tolist(),
            'pointSize': float(Config('mapping').voxel_size_m),
            'totalPoints': count,
            'robot': {'x': float(data['robot_pos'][0]),
                      'y': float(data['robot_pos'][1]),
                      'heading': float(data['robot_heading'])},
        }
    finally:
        reader.__exit__(None, None, None)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>BracketBot hand tracking</title>
<style>
body{margin:0;overflow:hidden;background:#0b0d10;color:#e8edf2;font:14px system-ui,sans-serif}
main{box-sizing:border-box;max-width:1320px;height:100dvh;margin:auto;padding:18px;display:flex;flex-direction:column}
h1{flex-shrink:0;font-size:22px;margin:0 0 12px}
img{display:block;width:100%;min-height:0;flex:1;object-fit:contain;background:#000;border-radius:10px}
#status{flex-shrink:0;margin:10px 0;color:#9fe3c2}pre{display:none}
</style></head><body><main><h1>Stereo hand tracking</h1>
<img src="/stream?annotated=true" alt="Camera 1 and camera 2 with hand landmarks">
<div id="status">starting…</div><pre id="data"></pre></main>
<script>
async function refresh(){try{const r=await fetch('/landmarks',{cache:'no-store'}),j=await r.json();
document.getElementById('status').textContent=`${j.status} · ${j.fps||0} fps · ${j.inference_ms||0} ms · camera 1: ${j.camera_1?.hand_count||0} hand(s) · camera 2: ${j.camera_2?.hand_count||0} hand(s) · 3D matches: ${j.stereo?.match_count||0} · thumbs up: ${j.stereo?.thumbs_up_count||0} · action: ${j.action_landmarking?.state||'unavailable'} · heard: ${j.action_landmarking?.label?.replaceAll('_',' ')||'none yet'} · voice: ${j.action_landmarking?.speech_status||'unavailable'}`;
const summary={action_landmarking:j.action_landmarking,stereo:j.stereo,camera_1:j.camera_1,camera_2:j.camera_2};document.getElementById('data').textContent=JSON.stringify(summary,null,2)}catch(e){document.getElementById('status').textContent=e}}
setInterval(refresh,500);refresh();
</script></body></html>"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8006)
    parser.add_argument("--model", type=Path, default=Path(os.getenv("REALBOT_HAND_MODEL", DEFAULT_MODEL)))
    parser.add_argument("--inference-width", type=int, default=640)
    parser.add_argument("--max-hands", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    global tracker
    args = parse_args()
    model_path = ensure_model(args.model.expanduser())
    tracker = StereoHandTracker(
        model_path=model_path,
        inference_width=args.inference_width,
        max_hands=args.max_hands,
    )
    tracker.start()
    try:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=2,
        )
    finally:
        tracker.close()


if __name__ == "__main__":
    main()
