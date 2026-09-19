# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["numpy==2.2.6", "opencv-python-headless==4.13.0.92"]
# ///
import json
import sys
from pathlib import Path

import cv2
import numpy as np

# Printed ChArUco board. Dimensions are in metres.
SQUARES = (7, 10)
SQUARE_LENGTH = 0.0223
MARKER_LENGTH = 0.0170
DICTIONARY = cv2.aruco.DICT_5X5_100
LEGACY_PATTERN = False


if __name__ == "__main__":
    folder = Path(sys.argv[1])
    output = Path(sys.argv[2])
    dictionary = cv2.aruco.getPredefinedDictionary(DICTIONARY)
    board = cv2.aruco.CharucoBoard(SQUARES, SQUARE_LENGTH, MARKER_LENGTH, dictionary)
    board.setLegacyPattern(LEGACY_PATTERN)
    detector = cv2.aruco.CharucoDetector(board)
    detections = []
    pairs_used = 0

    for pose_id, left_path in enumerate(sorted(folder.glob("camera0_*.png"))):
        right_name = left_path.name.replace("camera0_", "camera1_", 1)
        right_path = folder / right_name
        if not right_path.exists():
            print(f"Skipping {left_path.name}: no matching right image")
            continue

        pair = []
        pair_ids = []
        for camera_id, path in enumerate([left_path, right_path]):
            image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if image is None or image.shape != (960, 1280):
                raise ValueError(f"Expected a readable 1280x960 image: {path}")
            corners, ids, _, _ = detector.detectBoard(image)
            if ids is None:
                break

            features = []
            for i in range(len(ids)):
                corner_id = int(ids[i, 0])
                x = float(corners[i, 0, 0])
                y = float(corners[i, 0, 1])
                features.append({"id": corner_id, "point": {"x": x, "y": y}})
            pair.append({"cameraId": camera_id, "poseId": pose_id,
                         "targetId": 0, "featurePoints": features})
            pair_ids.append(ids.flatten().tolist())

        if len(pair) != 2:
            print(f"Skipping {left_path.name}: board not detected in both images")
            continue
        common_ids = []
        for corner_id in pair_ids[0]:
            if corner_id in pair_ids[1]:
                common_ids.append(corner_id)
        if len(common_ids) < 4 or board.checkCharucoCornersCollinear(np.array(common_ids, dtype=np.int32)):
            print(f"Skipping {left_path.name}: insufficient shared board corners")
            continue
        detections.extend(pair)
        pairs_used += 1
        print(f"{left_path.name}: {len(common_ids)} shared corners")

    if pairs_used == 0:
        raise ValueError("No usable stereo pairs. Check filenames, dictionary, and board layout.")

    object_points = []
    for x, y, z in board.getChessboardCorners():
        object_points.append({"x": float(x), "y": float(y), "z": float(z)})

    # Minimal Calib.io-compatible metadata for calibrate.py's existing reader.
    camera = {"model": {"ptr_wrapper": {"data": {
        "CameraModelCRT": {"CameraModelBase": {
            "imageSize": {"width": 1280, "height": 960}
        }}
    }}}}
    data = {
        "calibration": {
            "cameras": [camera, camera],
            "targets": [{"objectPoints": object_points}],
        },
        "detections": detections,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Wrote {pairs_used} stereo pairs to {output}")
