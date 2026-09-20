"""Run the local electrical-box ACT policy on robot 0188.

Changes vs live.py:
  * loads run-v3/best by default (--checkpoint to override)
  * after homing, ramps both arms to the DEMO start pose (the adapter home pose is
    up to ~23 units away from where the demonstrations begin)
  * runs at 10 Hz, the tick rate the demos were prepared at (--hz)
  * per-tick step limit comes from the demos' own per-100 ms command steps (p99),
    relative to the previous command, instead of a flat +-2 around the measured state
  * ACT temporal ensembling over overlapping chunks (--ensemble 0 to disable)
  * a stale camera frame holds the pose for that tick instead of aborting; only
    3 s of continuous staleness pauses the run (--stale)
Keys are unchanged: Space/Ctrl-C pause, R restarts a timed run, E cuts torque, Q parks/exits.
"""
import argparse
import io
import json
import math
import select
import signal
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path

import fcntl
policy_lock = open('/home/bracketbot/act-local/policy.lock', 'a')
try:
    fcntl.flock(policy_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit('Another ACT session owns the policy; stop it before starting another')

parser = argparse.ArgumentParser(description='Run the local electrical-box ACT policy on robot 0188. Homes both arms, ramps to the demo start pose, then runs the policy.')
parser.add_argument('--checkpoint', default='run-v3/best', metavar='DIR', help='checkpoint dir under ~/act-local (default: run-v3/best)')
parser.add_argument('--duration', type=float, default=120, metavar='SECONDS', help='policy run time after the start ramp; 0 runs until paused (default: 90)')
parser.add_argument('--hz', type=float, default=10, metavar='HZ', help='control rate; the demos are 10 Hz ticks (default: 10)')
parser.add_argument('--speed', type=float, default=2.0, help='scale on the per-tick step limit derived from the demos (default: 2.0; 1.0 = demo p99)')
parser.add_argument('--stale', type=float, default=1.0, metavar='SECONDS', help='observation age that holds the pose for that tick; 3 s of that pauses (default: 1.0)')
parser.add_argument('--ensemble', type=float, default=0.0, help='ACT temporal-ensembling coefficient, 0 disables (default: 0; it made the policy hesitate at the door)')
parser.add_argument('--chunk-steps', type=int, default=5, help='execute this many actions of each predicted chunk before re-planning (1 = every tick; >1 disables ensembling)')
parser.add_argument('--hold-start', action='store_true', help='no homing: take the arms where they are (e.g. right after teleop) and run the policy from there')
parser.add_argument('--yes', action='store_true', help='start without waiting for Enter')
parser.add_argument('--no-demo-pose', action='store_true', help='start the policy from the adapter home pose instead of the demo start pose')
parser.add_argument('--visitor-control', type=Path, help='Expiring visitor command file; starts from the current pose')
args = parser.parse_args()
visitor = None
if args.visitor_control:
    from visitor_control import VisitorControl
    visitor = VisitorControl(args.visitor_control)
    visitor.attempt = visitor.read()
    args.hold_start = args.yes = True
    visitor.report('loading')
for name, value in (('duration', args.duration), ('speed', args.speed), ('stale', args.stale), ('ensemble', args.ensemble)):
    if not math.isfinite(value) or value < 0:
        parser.error(f'--{name} must be finite and >= 0')
if not math.isfinite(args.hz) or args.hz <= 0:
    parser.error('--hz must be finite and > 0')

import numpy as np
from PIL import Image
import torch
import draccus

sys.path[:0] = ['/home/bracketbot/bbos', '/home/bracketbot/realbot/inference']
import bracketbot_adapter as robot
from lerobot.policies.act.modeling_act import ACTPolicy
from lerobot.configs.policies import PreTrainedConfig

root = Path('/home/bracketbot/act-local')
torch.set_num_threads(2)
checkpoint = root / args.checkpoint
run = checkpoint.parent
config = draccus.decode(PreTrainedConfig, {'type': 'act', **json.loads((checkpoint / 'config.json').read_text())})
config.n_action_steps = max(1, args.chunk_steps)          # 1 = re-plan every tick, >1 = open-loop through the chunk
config.temporal_ensemble_coeff = (args.ensemble or None) if args.chunk_steps <= 1 else None
policy = ACTPolicy.from_pretrained(checkpoint, config=config, strict=True).cuda().eval()
with np.load(run / 'stats.npz') as z:
    stats = {k: torch.tensor(z[k], device='cuda') for k in z.files}
names = list(robot.action_features)
assert len(names) == 16
episodes = sorted((root / 'prepared').glob('*.npz'))
with np.load(episodes[0]) as recorded:
    for side, low, high in [('left', robot.cal_l_min, robot.cal_l_max), ('right', robot.cal_r_min, robot.cal_r_max)]:
        assert np.allclose(recorded[f'cal_arm_{side}_min'], low)
        assert np.allclose(recorded[f'cal_arm_{side}_max'], high)

# Demo statistics: where the demos start, how far a command moves per 100 ms tick, how far it leads the state.
if (run / 'demo_pose.json').exists():
    demo = json.loads((run / 'demo_pose.json').read_text())
else:
    firsts, steps_, leads = [], [], []
    for p in episodes:
        with np.load(p) as z:
            firsts.append(z['state'][0])
            steps_.append(np.abs(np.diff(z['action'], axis=0)))
            leads.append(np.abs(z['action'] - z['state']))
    demo = {'start_mean': np.mean(firsts, 0).tolist(), 'start_std': np.std(firsts, 0).tolist(),
            'step_100ms_p99': np.percentile(np.concatenate(steps_), 99, axis=0).tolist(),
            'lead_p99': np.percentile(np.concatenate(leads), 99, axis=0).tolist()}
start_pose = np.array(demo['start_mean'], np.float32)
tick_s = 1 / args.hz
step_limit = np.maximum(np.array(demo['step_100ms_p99']) * (tick_s / 0.1) * args.speed, 0.5)
step_limit[[0, 8]] = np.minimum(step_limit[[0, 8]], 1.0)   # J0 (the lift) barely moves in the demos: keep it slow
band = np.maximum(np.array(demo['lead_p99']) * 1.5, 5.0)   # how far a command may lead the measured state
lows = np.array([-100] * 7 + [0] + [-100] * 7 + [0], np.float32)
highs = np.full(16, 100, np.float32)
print(json.dumps({'checkpoint': str(checkpoint), 'hz': args.hz, 'step_limit': step_limit.round(2).tolist(),
                  'band': band.round(1).tolist(), 'start_pose': start_pose.round(1).tolist()}), flush=True)

cams = ('head', 'arm_left', 'arm_right')
mean = torch.tensor([.485, .456, .406], device='cuda')[None, :, None, None]
std = torch.tensor([.229, .224, .225], device='cuda')[None, :, None, None]


def batch(obs):
    state = torch.tensor([[obs[k] for k in names]], device='cuda')
    result = {'observation.state': (state - stats['state_mean']) / stats['state_std']}
    for camera in cams:
        img = Image.open(io.BytesIO(obs[camera])).convert('RGB')
        if camera == 'head':
            img = img.crop((0, 0, img.width // 2, img.height))
        rgb = np.array(img.resize((224, 224), Image.Resampling.BILINEAR))
        tensor = torch.tensor(rgb, device='cuda').permute(2, 0, 1)[None].float() / 255
        result['observation.images.' + camera] = (tensor - mean) / std
    return result


def state_of(obs):
    return np.array([obs[k] for k in names], np.float32)


with np.load(episodes[0]) as z, torch.inference_mode():
    warm = {'observation.state': torch.zeros(1, 16, device='cuda')}
    for camera in cams:
        image = torch.tensor(z[camera][0:1], device='cuda').permute(0, 3, 1, 2).float() / 255
        warm['observation.images.' + camera] = (image - mean) / std
    assert torch.isfinite(policy.predict_action_chunk(warm)).all()
policy.reset()
print('MODEL READY.' + ('' if args.yes else ' Enter to park the previous ACT session, home both arms, ramp to the demo start pose, and start.'), flush=True)
if not args.yes:
    input()
from bbos import Reader
for session in (() if visitor else ('act-live-0188', 'act-live-continuous', 'act-live-v2', 'act-home-0188')):
    if subprocess.run(['tmux', 'has-session', '-t', session], capture_output=True).returncode == 0:
        subprocess.run(['tmux', 'send-keys', '-t', session, 'C-c' if session == 'act-home-0188' else 'q'], check=True)
        deadline = time.monotonic() + 30
        while subprocess.run(['tmux', 'has-session', '-t', session], capture_output=True).returncode == 0:
            if time.monotonic() >= deadline:
                raise RuntimeError(f'{session} did not finish parking; no new controller started')
            time.sleep(.1)
for side in ('left', 'right'):
    reader = Reader(f'arm_{side}.ctrl', keeptime=False).__enter__()
    try:
        deadline = time.monotonic() + 5
        while True:
            reader.ready()
            if not reader.readable:
                break
            if time.monotonic() >= deadline:
                if args.hold_start:
                    print(f'[hold-start] arm_{side}.ctrl writer slot still registered '
                          f'(killed teleop); taking the arms over anyway', flush=True)
                    break
                raise RuntimeError(f'Another controller owns arm_{side}; stop it first')
            time.sleep(.1)
    finally:
        reader.__exit__(None, None, None)
def hold_start():
    """Take the arms where they are (e.g. right after teleop was killed): open the adapter's readers
    and writers without homing, command the current pose, keep torque on."""
    from bbos import Reader, Writer, Type
    robot._readers['left'] = Reader('arm_left.state').__enter__()
    robot._readers['right'] = Reader('arm_right.state').__enter__()
    robot._readers['cam_left'] = Reader('camera.left.jpeg').__enter__()
    robot._readers['cam_right'] = Reader('camera.right.jpeg').__enter__()
    robot._readers['cam_head'] = Reader('camera.head.jpeg').__enter__()
    robot._writers['torque_l'] = Writer('arm_left.torque', Type('arm_torque')).__enter__()
    robot._writers['ctrl_l'] = Writer('arm_left.ctrl', Type('arm_ctrl')).__enter__()
    robot._writers['torque_r'] = Writer('arm_right.torque', Type('arm_torque')).__enter__()
    robot._writers['ctrl_r'] = Writer('arm_right.ctrl', Type('arm_ctrl')).__enter__()
    for side, key in (('l', 'left'), ('r', 'right')):
        r = robot._readers[key]
        while not r.ready():
            time.sleep(.005)
        pos = np.array(r.data['pos'], np.float32)
        robot._writers[f'ctrl_{side}']['pos'] = pos
        with robot._writers[f'torque_{side}'].buf() as b:
            b['enable'][:] = np.ones(len(pos), np.bool_)
    print('[hold-start] arms taken over in place, torque on', flush=True)


if visitor:
    while not visitor.tick()[0]:
        time.sleep(.1)
if args.hold_start:
    hold_start()
else:
    robot.connect()


def ramp_to(target, seconds):
    """Joint-space smoothstep ramp from the current pose to target, torque on, 50 Hz."""
    start = state_of(robot.get_observation())
    t0 = time.monotonic()
    while True:
        a = min((time.monotonic() - t0) / seconds, 1.0)
        s = a * a * (3 - 2 * a)
        robot.send_action(dict(zip(names, (start + s * (target - start)).tolist())))
        if a >= 1.0:
            break
        time.sleep(.02)


paused = False
def pause(*_):
    global paused
    paused = True
    if visitor:
        visitor.hold()
signal.signal(signal.SIGINT, pause)
signal.signal(signal.SIGTERM, pause)
signal.signal(signal.SIGUSR1, pause)
settings = termios.tcgetattr(sys.stdin)
tty.setcbreak(sys.stdin.fileno())
if not args.no_demo_pose and not args.hold_start:
    print('RAMPING to the demo start pose over 4 s ...', flush=True)
    ramp_to(start_pose, 4.0)
    time.sleep(.5)
print(f'RUNNING at up to {args.hz:g} Hz for {args.duration:g}s (0=unlimited). Space/Ctrl-C pauses, R restarts from here, N ramps back to the demo start pose and restarts, E cuts torque, Q twice parks/exits; leave tmux with Ctrl-b d.', flush=True)
started = time.monotonic()
last_log = -1
last_sent = None
steps = 0
target = None
stale_since = None
clamped = 0
last_q = -10.0
try:
    with torch.inference_mode():
        while True:
            tick = time.monotonic()
            if visitor:
                active, restart = visitor.tick()
                paused = not active
                if restart:
                    policy.reset()
                    target = last_sent = stale_since = None
                    started = tick
                    last_log, steps, clamped = -1, 0, 0
            if select.select([sys.stdin], [], [], 0)[0]:
                key = sys.stdin.read(1).lower()
                if visitor and key not in ('e', 'q'):
                    key = ' '  # Remote attempts start only through a fresh leased command.
                if key == 'e': robot._estop_now()
                if key == 'q':
                    if time.monotonic() - last_q > 2.0:
                        last_q = time.monotonic()
                        print('press Q again within 2 s to park and exit (to leave tmux without stopping, use Ctrl-b then d)', flush=True)
                        continue
                    robot.disconnect()
                    break
                if key == 'n':                       # new attempt: back to the demo start pose, then run again
                    paused = True
                    target = None
                    print('NEW ATTEMPT: ramping to the demo start pose over 4 s ...', flush=True)
                    ramp_to(start_pose, 4.0)
                    time.sleep(.3)
                    key, paused = 'r', True
                if key == 'r' and paused:
                    policy.reset()
                    paused = False
                    target = None
                    last_sent = None
                    stale_since = None
                    started = time.monotonic()
                    last_log = -1
                    steps = 0
                    clamped = 0
                    print(f'RESUMED at up to {args.hz:g} Hz', flush=True)
                elif key != 'r':
                    pause()
            obs = robot.get_observation()
            if args.duration and time.monotonic() - started >= args.duration:
                pause()
            if paused:
                if target is None:
                    target = {k: obs[k] for k in names}
                    print(f'PAUSED after {steps} actions. Holding pose; R resumes.', flush=True)
                robot.send_action(target)
                time.sleep(.02)
                continue
            current = state_of(obs)
            ages = [(time.time_ns() - int(r.data['timestamp'].astype('int64'))) / 1e9 for r in robot._readers.values()]
            fresh = all(r.readable for r in robot._readers.values()) and 0 <= max(ages) < args.stale
            if not fresh:
                stale_since = stale_since or tick
                if tick - stale_since > 3.0:
                    raise RuntimeError(f'Stale observation for 3 s: {max(ages):.3f}s')
                hold = current if last_sent is None else last_sent
                robot.send_action(dict(zip(names, hold.tolist())))
                time.sleep(max(0, tick_s - (time.monotonic() - tick)))
                continue
            stale_since = None
            raw = policy.select_action(batch(obs))[0] * stats['action_std'] + stats['action_mean']
            raw = raw.float().cpu().numpy()
            if not np.isfinite(raw).all(): raise RuntimeError('Nonfinite policy action')
            ref = current if last_sent is None else last_sent
            action = np.clip(raw, ref - step_limit, ref + step_limit)      # demo-scale step per tick
            action = np.clip(action, current - band, current + band)      # never lead the arm further than the demos did
            action = np.clip(action, lows, highs)
            clamped += int(np.any(np.abs(action - raw) > 1e-3))
            if (paused or (visitor and visitor.read() != visitor.attempt)
                    or (args.duration and time.monotonic() - started >= args.duration)):
                pause()
                continue
            last_sent = action
            robot.send_action(dict(zip(names, action.tolist())))
            steps += 1
            second = int(time.monotonic() - started)
            if second != last_log:
                print(json.dumps({'second': second, 'steps': steps, 'clamped_steps': clamped, 'max_obs_age': round(max(ages), 3),
                                  'state': current.round(2).tolist(), 'raw': raw.round(2).tolist(), 'sent': action.round(2).tolist()}), flush=True)
                for camera in cams:
                    (root / f'live-{camera}.jpg').write_bytes(obs[camera])
                last_log = second
            time.sleep(max(0, tick_s - (time.monotonic() - tick)))
except Exception as exc:
    if visitor:
        visitor.hold()
        visitor.report('error', str(exc))
    print('PAUSING:', repr(exc), flush=True)
finally:
    if sys.exc_info()[0] is SystemExit or not robot._writers:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, settings)
        sys.exit()
    observation = robot.get_observation()
    target = {k: observation[k] for k in names}
    print(f'PAUSED after {steps} actions. Holding current pose. Q parks, E cuts torque.', flush=True)
    while True:
        if visitor:
            visitor.report('error', 'Policy stopped after an error; operator restart required')
        if select.select([sys.stdin], [], [], 0)[0]:
            key = sys.stdin.read(1).lower()
            if key == 'e': robot._estop_now()
            if key == 'q':
                if time.monotonic() - last_q > 2.0:
                    last_q = time.monotonic()
                    print('press Q again within 2 s to park and exit (to leave tmux without stopping, use Ctrl-b then d)', flush=True)
                    continue
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, settings)
                robot.disconnect()
                break
        robot.send_action(target)
        time.sleep(.02)
