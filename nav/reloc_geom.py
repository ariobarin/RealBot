"""Frame + floor-fit helpers for reloc_nav.

The slam-reloc stack (p_slam) localizes in the Auki/COLMAP map and publishes slam.pose as
the robot BASE pose in the GL frame (COLMAP -> GL via MW, GL up = +Y). That GL +Y is exactly
the up-vector the reloc uprightness/"floor lock-in" uses (MAP_UP_COLMAP = [1,0,0]).

This module turns that Y-up map into a z-up NAV frame the v23 planner/controller expect:
  - fit the floor height along MAP_UP (densest low bin, robust to sky/far outliers),
  - build T_nav_gl that sends MAP_UP -> +Z and drops the floor to z=0,
  - validate the fitted band's true normal against MAP_UP (the lock-in convention),
  - rasterize the COLMAP cloud into a square static occupancy grid (0 unknown / 1 floor /
    2 obstacle) for the planner; the planner's own ROBOT_RADIUS_CELLS inflation is the margin.
"""
import numpy as np

# GL up = MW @ MAP_UP_COLMAP[1,0,0] = [0,1,0]; the reloc floor lock-in vertical.
MAP_UP_GL = np.array([0., 1., 0.])
# COLMAP -> GL (cv2gl), matches p_slam / p_slam_viz. Portal poses live in COLMAP and need this.
MW = np.array([[0, 1, 0], [1, 0, 0], [0, 0, -1]], float)


def _rot_a_to_b(a, b):
    """Rotation matrix sending unit vector a -> unit vector b (Rodrigues)."""
    a = a / np.linalg.norm(a); b = b / np.linalg.norm(b)
    v = np.cross(a, b); c = float(a @ b); s = np.linalg.norm(v)
    if s < 1e-9:
        return np.eye(3) if c > 0 else np.diag([1., -1., -1.])
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0.]])
    return np.eye(3) + vx + vx @ vx * ((1 - c) / (s * s))


def fit_floor_nav(xyz, up=MAP_UP_GL, band=0.12, clip_pct=2.0):
    """Find floor height along `up` and build T_nav_gl (4x4) sending up->+Z, floor->z=0.

    Normal is CONSTRAINED to `up` (the lock-in convention) — only the floor height is fit, so a
    few tilted/far points can't skew the frame. Returns (T_nav_gl, info) with a validation tilt
    (angle between the fitted band's SVD normal and `up`) the caller should sanity-check/log.
    """
    P = xyz.astype(np.float64)
    lo, hi = np.percentile(P, clip_pct, 0), np.percentile(P, 100 - clip_pct, 0)
    Pin = P[np.all((P >= lo) & (P <= hi), 1)]
    h = Pin @ up
    hist, edges = np.histogram(h, bins=120); ctr = (edges[:-1] + edges[1:]) / 2
    sub = np.where(hist > hist.max() * 0.15)[0]
    floor_h = float(ctr[sub[0]])                       # lowest substantial surface = floor
    Q = Pin[np.abs(h - floor_h) < band]
    cov = np.cov((Q - Q.mean(0)).T)          # 3x3 — eigvec of smallest eigval is the plane normal
    nrm = np.linalg.eigh(cov)[1][:, 0]
    if nrm @ up < 0: nrm = -nrm
    tilt = float(np.degrees(np.arccos(np.clip(abs(nrm @ up), 0, 1))))
    R = _rot_a_to_b(up, np.array([0., 0., 1.]))        # up -> +Z (constrained)
    T = np.eye(4); T[:3, :3] = R
    T[2, 3] = -float((R @ (floor_h * up))[2])          # floor plane -> z = 0
    return T, {"floor_h": floor_h, "tilt_deg": tilt, "band_pts": int(len(Q)), "n_in": int(len(Pin))}


def apply(T, pts):
    """Apply a 4x4 transform to an (N,3) array."""
    return pts @ T[:3, :3].T + T[:3, 3]


def quat_to_mat(pos, quat):
    """(pos xyz, quat xyzw) -> 4x4. Local impl so the module has no scipy dependency."""
    x, y, z, w = quat
    n = x * x + y * y + z * z + w * w
    if n < 1e-12:
        R = np.eye(3)
    else:
        x, y, z, w = (x, y, z, w) / np.sqrt(n)
        R = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w),     2 * (x * z + y * w)],
            [2 * (x * y + z * w),     1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w),     2 * (y * z + x * w),     1 - 2 * (x * x + y * y)],
        ])
    T = np.eye(4); T[:3, :3] = R; T[:3, 3] = pos
    return T


def yaw_from_R(R):
    """Nav-frame yaw (about +Z) matching the v23 controller convention forward = base +Y,
    where heading = (-sin yaw, cos yaw)."""
    f = R @ np.array([0., 1., 0.])
    return float(np.arctan2(-f[0], f[1]))


def build_static_grid(cloud_nav, res, floor_band=0.08, obs_lo=0.20, obs_hi=1.6,
                      pad_m=1.0, gs_cap=2200, min_obs=3, obs_blob=30, fill_iters=8):
    """Rasterize the nav-frame cloud into a SQUARE occupancy grid (Dijkstra needs square):
    0 unknown / 1 floor / 2 obstacle. Returns (grid, origin_xy, gs).

    The raw COLMAP floor is sparse dust and the >obs_lo points include scattered noise, so a
    direct rasterize erodes to nothing. Instead: count points per cell, call a cell an obstacle
    only with >=min_obs points and drop obstacle blobs <obs_blob cells (keeps walls, kills dust);
    FILL the floor footprint (closing+fill-holes) and keep its largest connected component. The
    planner's ROBOT_RADIUS_CELLS erosion then supplies the placement/clearance margin.
    """
    from scipy.ndimage import binary_closing, binary_fill_holes, label
    z = cloud_nav[:, 2]
    fp = cloud_nav[(z > -floor_band) & (z < floor_band)]
    if len(fp) == 0:
        raise RuntimeError("floor fit: no cloud points near z=0")
    xmin, ymin = float(fp[:, 0].min()) - pad_m, float(fp[:, 1].min()) - pad_m
    xmax, ymax = float(fp[:, 0].max()) + pad_m, float(fp[:, 1].max()) + pad_m
    gs = min(int(np.ceil(max(xmax - xmin, ymax - ymin) / res)) + 1, gs_cap)
    origin = np.array([xmin, ymin], np.float32)

    def count(P):
        g = np.zeros((gs, gs), np.int32)
        gi = np.floor((P[:, 0] - xmin) / res).astype(np.int64)
        gj = np.floor((P[:, 1] - ymin) / res).astype(np.int64)
        ok = (gi >= 0) & (gi < gs) & (gj >= 0) & (gj < gs)
        np.add.at(g, (gi[ok], gj[ok]), 1)
        return g

    # obstacles = dense columns above the floor, with dust blobs removed (keep walls)
    obs = count(cloud_nav[(z > obs_lo) & (z < obs_hi)]) >= min_obs
    lbl, n = label(obs)
    if n:
        keep = np.where(np.bincount(lbl.ravel()) >= obs_blob)[0]
        obs = np.isin(lbl, keep[keep > 0])

    # floor = filled footprint of the (sparse) floor points, minus obstacles, largest component
    floor = binary_fill_holes(binary_closing(count(fp) > 0, iterations=fill_iters)) & ~obs
    l2, n2 = label(floor)
    if n2:
        floor = l2 == (np.argmax(np.bincount(l2.ravel())[1:]) + 1)

    grid = np.zeros((gs, gs), np.uint8)
    grid[floor] = 1
    grid[obs] = 2
    return grid, origin, gs
