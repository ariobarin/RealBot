# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["numpy==2.2.6", "opencv-python-headless==4.13.0.92", "scipy==1.16.3"]
# ///
"""Calibrate from detected corners using OpenCV initialization and a joint SciPy fit."""
import json
import sys
from pathlib import Path

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix
from scipy.spatial.transform import Rotation


# Each camera has five parameters: f, cx, cy, k1, k2.
# This matches the reference: fx = fy, skew = 0, k3 = k4 = 0.
def camera_matrices(parameters):
    f, cx, cy, k1, k2 = parameters
    K = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1]])
    D = np.array([k1, k2, 0, 0])
    return K, D


def read_points(path):
    with open(path) as file:
        data = json.load(file)
    calibration = data.get("calibration")
    size = (1280, 960)

    observations = []
    pose_ids = {}
    for detection in data["detections"]:
        view_id = (detection["poseId"], detection["targetId"])
        if view_id not in pose_ids:
            pose_ids[view_id] = len(pose_ids)
        board = calibration["targets"][detection["targetId"]]["objectPoints"]
        objects = []
        pixels = []
        for feature in detection["featurePoints"]:
            point = board[feature["id"]]
            objects.append([point["x"], point["y"], point["z"]])
            pixel = feature["point"]
            pixels.append([pixel["x"], pixel["y"]])
        observations.append({
            "camera": detection["cameraId"],
            "pose": pose_ids[view_id],
            "objects": np.array(objects, dtype=float),
            "pixels": np.array(pixels, dtype=float),
        })
    return size, observations


def initialize(size, observations):
    parameters = []
    camera_poses = []
    flags = (
        cv2.fisheye.CALIB_RECOMPUTE_EXTRINSIC
        | cv2.fisheye.CALIB_FIX_SKEW
        | cv2.fisheye.CALIB_FIX_K3
        | cv2.fisheye.CALIB_FIX_K4
    )
    for camera_id in [0, 1]:
        objects = []
        pixels = []
        pose_ids = []
        for observation in observations:
            if observation["camera"] == camera_id:
                objects.append(observation["objects"].reshape(1, -1, 3))
                pixels.append(observation["pixels"].reshape(1, -1, 2))
                pose_ids.append(observation["pose"])

        _, K, D, rotations, translations = cv2.fisheye.calibrate(
            objects, pixels, size, None, None, flags=flags,
        )
        focal_length = (K[0, 0] + K[1, 1]) / 2
        parameters.extend([focal_length, K[0, 2], K[1, 2], D[0, 0], D[1, 0]])
        poses = {}
        for pose_id, rotation, translation in zip(pose_ids, rotations, translations):
            poses[pose_id] = (Rotation.from_rotvec(rotation.ravel()), translation.ravel())
        camera_poses.append(poses)

    # Each shared capture supplies an estimate of the left-to-right transform.
    stereo_rotations = []
    stereo_translations = []
    for pose_id, left_pose in camera_poses[0].items():
        if pose_id in camera_poses[1]:
            left_rotation, left_translation = left_pose
            right_rotation, right_translation = camera_poses[1][pose_id]
            rotation = right_rotation * left_rotation.inv()
            translation = right_translation - rotation.apply(left_translation)
            stereo_rotations.append(rotation.as_rotvec())
            stereo_translations.append(translation)
    stereo_rotation = Rotation.from_rotvec(stereo_rotations).mean()
    stereo_translation = np.median(stereo_translations, axis=0)
    parameters.extend(stereo_rotation.as_rotvec())
    parameters.extend(stereo_translation)

    # All board poses are expressed in the left camera's coordinates.
    pose_ids = set(camera_poses[0]) | set(camera_poses[1])
    for pose_id in sorted(pose_ids):
        if pose_id in camera_poses[0]:
            rotation, translation = camera_poses[0][pose_id]
        else:
            rotation, translation = camera_poses[1][pose_id]
            rotation = stereo_rotation.inv() * rotation
            translation = stereo_rotation.inv().apply(translation - stereo_translation)
        parameters.extend(rotation.as_rotvec())
        parameters.extend(translation)
    return np.array(parameters)


def residuals(parameters, observations):
    left_K, left_D = camera_matrices(parameters[:5])
    right_K, right_D = camera_matrices(parameters[5:10])
    stereo_rotation = Rotation.from_rotvec(parameters[10:13])
    stereo_translation = parameters[13:16]
    board_poses = parameters[16:].reshape(-1, 6)
    errors = []

    for observation in observations:
        pose = board_poses[observation["pose"]]
        rotation = Rotation.from_rotvec(pose[:3])
        translation = pose[3:]
        K, D = left_K, left_D
        if observation["camera"] == 1:
            translation = stereo_rotation.apply(translation) + stereo_translation
            rotation = stereo_rotation * rotation
            K, D = right_K, right_D

        predicted, _ = cv2.fisheye.projectPoints(
            observation["objects"].reshape(1, -1, 3),
            rotation.as_rotvec(), translation, K, D,
        )
        error = predicted.reshape(-1, 2) - observation["pixels"]
        errors.extend(error.ravel())
    return np.array(errors)


def jacobian_sparsity(observations, parameter_count):
    # Tell SciPy which parameters affect each error, avoiding dense differences.
    error_count = 0
    for observation in observations:
        error_count += 2 * len(observation["pixels"])
    pattern = lil_matrix((error_count, parameter_count), dtype=int)
    row = 0
    for observation in observations:
        end = row + 2 * len(observation["pixels"])
        camera_start = 5 * observation["camera"]
        pose_start = 16 + 6 * observation["pose"]
        pattern[row:end, camera_start:camera_start + 5] = 1
        pattern[row:end, pose_start:pose_start + 6] = 1
        if observation["camera"] == 1:
            pattern[row:end, 10:16] = 1
        row = end
    return pattern.tocsr()


if __name__ == "__main__":
    source = Path(sys.argv[1])
    output = Path(sys.argv[2])
    size, observations = read_points(source)
    initial = initialize(size, observations)
    initial_errors = residuals(initial, observations)
    point_count = len(initial_errors) // 2
    print(f"Fitting all {point_count} detected corners", flush=True)

    result = least_squares(
        residuals, initial, args=(observations,),
        jac_sparsity=jacobian_sparsity(observations, len(initial)),
        x_scale="jac", verbose=1,
    )

    K1, D1 = camera_matrices(result.x[:5])
    K2, D2 = camera_matrices(result.x[5:10])
    R = Rotation.from_rotvec(result.x[10:13]).as_matrix()
    T = result.x[13:16].reshape(3, 1) * 1000  # metres to millimetres
    R1, R2, P1, P2, Q = cv2.fisheye.stereoRectify(
        K1, D1, K2, D2, size, R, T, flags=cv2.CALIB_ZERO_DISPARITY,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    storage = cv2.FileStorage(str(output), cv2.FILE_STORAGE_WRITE)
    storage.write("mtx_l", K1)
    storage.write("dist_l", D1.reshape(4, 1))
    storage.write("mtx_r", K2)
    storage.write("dist_r", D2.reshape(4, 1))
    storage.write("R", R)
    storage.write("T", T)
    storage.write("R1", R1)
    storage.write("R2", R2)
    storage.write("P1", P1)
    storage.write("P2", P2)
    storage.write("Q", Q)
    storage.release()
    before = np.sqrt(np.sum(initial_errors ** 2) / point_count)
    after = np.sqrt(np.sum(result.fun ** 2) / point_count)
    print(f"Reprojection RMS: {before:.4f} -> {after:.4f} pixels")
    print(f"Wrote {output}")
