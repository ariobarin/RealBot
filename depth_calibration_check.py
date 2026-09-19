# /// script
# requires-python = ">=3.10,<3.11"
# dependencies = [
#   "bbos",
#   "numpy",
#   "opencv-python-headless",
# ]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""Checks the depth stereo calibration against a live camera frame.

Run with:  uv run ~/bbapps/depth_calibration_check.py   (camera daemon must be running)
"""
import contextlib, io, os, sys, time
import numpy as np
import cv2
with contextlib.redirect_stdout(io.StringIO()):
    from bbos import Reader, Config

CFG_C = Config("cam_head")
src_w, src_h = CFG_C.width // 2, CFG_C.height

TOPIC = next((t for t in ("camera.head.jpeg", "camera.head_jpeg")
              if os.path.exists(f"/dev/shm/{t}")), "camera.head.jpeg")

with Reader(TOPIC, keeptime=False, sync=True) as r:
    t0 = time.time()
    while not r.ready():
        if time.time() - t0 > 12:
            print("no camera frame — is the camera daemon running?"); sys.exit(2)
    n = int(r.data['jpeg_len'])
    stereo = cv2.imdecode(r.data['jpeg'][:n], cv2.IMREAD_COLOR)
if stereo is None:
    print("could not decode camera jpeg frame"); sys.exit(2)
L = np.ascontiguousarray(stereo[:, :src_w]); Rt = np.ascontiguousarray(stereo[:, src_w:])

orb = cv2.ORB_create(6000); bf = cv2.BFMatcher(cv2.NORM_HAMMING)

def epi_error(gl, gr):
    """Median vertical error (px) of L<->R feature matches, or None."""
    k1, d1 = orb.detectAndCompute(gl, None); k2, d2 = orb.detectAndCompute(gr, None)
    if d1 is None or d2 is None: return None, 0
    good = [m for m, nn in bf.knnMatch(d1, d2, k=2) if m.distance < 0.75 * nn.distance]
    if len(good) < 20: return None, len(good)
    dy = np.array([k1[m.queryIdx].pt[1] - k2[m.trainIdx].pt[1] for m in good])
    return float(np.median(np.abs(dy))), len(good)

CFG_D = Config("depth")
mtx_l, dist_l, mtx_r, dist_r, R1, R2, P1, P2, Q, base, fx, R, t = CFG_D.camera_cal()
m1x, m1y = cv2.fisheye.initUndistortRectifyMap(mtx_l, dist_l[:4].reshape(4,1), R1, P1, (src_w, src_h), cv2.CV_32FC1)
m2x, m2y = cv2.fisheye.initUndistortRectifyMap(mtx_r, dist_r[:4].reshape(4,1), R2, P2, (src_w, src_h), cv2.CV_32FC1)

raw_err, raw_n = epi_error(cv2.cvtColor(L, cv2.COLOR_BGR2GRAY), cv2.cvtColor(Rt, cv2.COLOR_BGR2GRAY))
rect_err, rect_n = epi_error(cv2.cvtColor(cv2.remap(L, m1x, m1y, cv2.INTER_LINEAR), cv2.COLOR_BGR2GRAY),
                             cv2.cvtColor(cv2.remap(Rt, m2x, m2y, cv2.INTER_LINEAR), cv2.COLOR_BGR2GRAY))

print("Depth calibration check")
print(f"  camera:       {src_w}x{src_h} per eye")
print(f"  calibration:  {CFG_D.calib_path}  (baseline {base*1000:.1f}mm)")
if raw_err is None or rect_err is None:
    print("  not enough visual features to judge — point the robot at a textured scene and rerun")
    sys.exit(2)
print(f"  error before rectification:  {raw_err:.1f}px")
print(f"  error after rectification:   {rect_err:.2f}px  ({rect_n} features)")
print()
if rect_err < 1.0:
    print("status: GOOD")
elif rect_err <= 1.5:
    print("status: OKAY")
else:
    print("status: BAD — recalibrate the camera")
    sys.exit(1)
