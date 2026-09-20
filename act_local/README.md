# act_local: on-robot ACT policy for "open the electrical box" (robot 0188)

Everything here runs directly on the robot's Jetson Orin Nano. The deployed copy lives at
`/home/bracketbot/act-local` (the scripts hardcode that path); this directory is the
version-controlled source for it.

## Result (2026-09-20)

`run-v3/best` (step 1000 of `train3.py`) opens the box when started from the demo start
pose with `live2.py`'s defaults. Validation L1 is 0.508 against 0.574 for holding still.
It sometimes misses the latch hole from a fresh start; the guided path (Quest teleop to the
hole, then hand off) works reliably. See `run-v3/log.jsonl` for the curve.

## Pipeline

| Step | Script | Notes |
|---|---|---|
| 1. Fetch | `fetch.py` | Downloads the `quest_teleop/open_electrical_box_0188_v2` episodes + videos into `raw/`. |
| 2. Prepare | `prepare.py` | Resamples each episode onto 10 Hz ticks, normalizes joints to the adapter's [-100, 100] / [0, 100] ranges, decodes the three cameras to 224x224 (head = left half of the stereo pair). Writes `prepared/*.npz`. |
| 3. Train | `train3.py` | Recommended on the Jetson: freezes the ResNet18 backbone, precomputes its feature maps once (`cache/feat_*.npy`), trains the transformer at batch 16, ~2 steps/s. Keeps `<out>/best` (lowest validation L1) and `<out>/last`. |
|    | `train2.py` | Full fine-tune at batch 8 with a memory-mapped data cache, ~1.1 s/step. Use overnight. |
|    | `train.py` | The original batch-1 trainer. Kept for reference; it produced a model worse than holding still. |
| 4. Run | `live2.py` | Live driver. Homes, ramps to the demo start pose, runs the policy. Defaults are the settings that opened the box. |
|    | `demo.sh` | One-shot demo: parks anything running, then `live2.py --yes` (fresh) or teleop + waiting policy (`demo.sh guided`). |
|    | `go.sh` | Guided mode handoff: kills teleop without parking, starts the waiting policy from the current pose. |

`pipeline.sh`, `benchmark.py`, `arm_owner.py` and `live.py` are helpers: the original
prepare-then-train runner, a timing/memory probe, an arm-writer ownership check, and the
original live driver kept for comparison.

## Running the demo

From any machine that can SSH to the robot:

```bash
ssh bracketbot@192.168.2.29 '~/act-local/demo.sh'            # fresh: home, ramp, run 120 s
ssh bracketbot@192.168.2.29 '~/act-local/demo.sh guided'     # Quest teleop + pre-loaded policy
ssh bracketbot@192.168.2.29 '~/act-local/go.sh'              # guided: hand off once the finger is in the hole
```

Watch or intervene with `tmux a -t act-v3` on the robot: E cuts torque, Space pauses,
R restarts a timed run, Q parks and exits.

## Why the first attempt failed, and what `live2.py` changes

* `train.py` ran 2000 steps at batch size 1. Its validation error (0.68) was worse than
  simply holding the current pose (0.40), so the policy had not learned the task.
* `live.py` homed to the adapter's home pose, which is up to 23 units (of 200) away from
  where the demonstrations start, so the policy spent the run crawling toward the demo pose.
* `live.py` clamped every joint to +-2 units per tick at 5 Hz; the demos move up to 10
  units per 100 ms. Nearly every command saturated the limiter.
* `live.py` raised on any observation older than 0.5 s; the training data itself contains
  frames up to 0.42 s old, so runs died after ~45 s.

`live2.py` starts from the demo start pose, runs at the demos' 10 Hz, derives per-tick step
limits from the demos' own command steps, executes 5 actions per predicted chunk (temporal
ensembling made it stall at the door), and holds pose on a stale frame instead of aborting.
`--hold-start` skips homing and takes the arms wherever teleop left them.

## Environment on the robot

`/home/bracketbot/act-local/.venv` is Python 3.10 with `lerobot==0.4.3`, `torchvision==0.26`,
`draccus`, `av`, `Pillow`, `ultralytics` and `torch` resolved from
`/home/bracketbot/realbot/inference/.venv` via a `.pth` file. `bbos` is imported from
`/home/bracketbot/bbos`. Training needs about 1.1 GB of GPU memory; leave the rest of the
8 GB to the bbOS daemons.
