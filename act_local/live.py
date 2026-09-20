import argparse
import math
import io
import json
from pathlib import Path
import select
import signal
import sys
import termios
import time
import tty

parser = argparse.ArgumentParser(description='Run local ACT on robot 0188. Homes both arms on start.')
parser.add_argument('--duration', type=float, default=60, metavar='SECONDS',
                    help='policy run time after homing; 0 runs until paused (default: 60)')
parser.add_argument('--hz', type=float, default=5, metavar='HZ',
                    help='maximum inference/action updates per second; any positive finite rate (default: 5)')
args = parser.parse_args()
if not math.isfinite(args.duration) or args.duration < 0:
    parser.error('--duration must be finite and >= 0')
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
checkpoint = root / 'run-v1/step-1750'
config = draccus.decode(PreTrainedConfig, {'type':'act', **json.loads((checkpoint/'config.json').read_text())})
policy = ACTPolicy.from_pretrained(checkpoint, config=config, strict=True).cuda().eval()
policy.config.n_action_steps = 1
with np.load(root / 'run-v1/stats.npz') as z:
    stats = {k: torch.tensor(z[k], device='cuda') for k in z.files}
names = list(robot.action_features)
assert len(names) == 16
with np.load(root/'prepared/ep000000_6d9e1e19_0.npz') as recorded:
    for side, low, high in [('left',robot.cal_l_min,robot.cal_l_max),('right',robot.cal_r_min,robot.cal_r_max)]:
        assert np.allclose(recorded[f'cal_arm_{side}_min'],low)
        assert np.allclose(recorded[f'cal_arm_{side}_max'],high)
cams = ('head', 'arm_left', 'arm_right')
mean = torch.tensor([.485,.456,.406], device='cuda')[None,:,None,None]
std = torch.tensor([.229,.224,.225], device='cuda')[None,:,None,None]

def batch(obs):
    state = torch.tensor([[obs[k] for k in names]], device='cuda')
    result = {'observation.state':(state-stats['state_mean'])/stats['state_std']}
    for camera in cams:
        img = Image.open(io.BytesIO(obs[camera])).convert('RGB')
        if camera == 'head':
            img = img.crop((0,0,img.width//2,img.height))
        rgb = np.array(img.resize((224,224),Image.Resampling.BILINEAR))
        tensor = torch.tensor(rgb,device='cuda').permute(2,0,1)[None].float()/255
        result['observation.images.'+camera] = (tensor-mean)/std
    return result

with np.load(root/'prepared/ep000000_6d9e1e19_0.npz') as z, torch.inference_mode():
    warm = {'observation.state':torch.zeros(1,16,device='cuda')}
    for camera in cams:
        image = torch.tensor(z[camera][0:1],device='cuda').permute(0,3,1,2).float()/255
        warm['observation.images.'+camera]=(image-mean)/std
    assert torch.isfinite(policy.predict_action_chunk(warm)).all()
policy.reset()
print('MODEL READY. Enter to park the previous ACT session, home both arms, and start.',flush=True)
input()
import subprocess
from bbos import Reader
for session in ('act-live-0188','act-live-continuous','act-home-0188'):
    if subprocess.run(['tmux','has-session','-t',session],capture_output=True).returncode == 0:
        subprocess.run(['tmux','send-keys','-t',session,'C-c' if session == 'act-home-0188' else 'q'],check=True)
        deadline = time.monotonic()+30
        while subprocess.run(['tmux','has-session','-t',session],capture_output=True).returncode == 0:
            if time.monotonic() >= deadline:
                raise RuntimeError(f'{session} did not finish parking; no new controller started')
            time.sleep(.1)
for side in ('left','right'):
    with Reader(f'arm_{side}.ctrl',keeptime=False) as reader:
        reader.ready()
        if reader.readable:
            raise RuntimeError(f'Another controller owns arm_{side}; stop it first')
robot.connect()
paused = False
def pause(*args):
    global paused
    paused = True
signal.signal(signal.SIGINT,pause)
signal.signal(signal.SIGTERM,pause)
signal.signal(signal.SIGUSR1,pause)
settings = termios.tcgetattr(sys.stdin)
tty.setcbreak(sys.stdin.fileno())
print(f'RUNNING at up to {args.hz:g} Hz for {args.duration:g}s (0=unlimited). Space/Ctrl-C pauses, R starts a fresh timed run, E cuts torque, Q parks/exits.',flush=True)
started = time.monotonic()
last_log = -1
last_sent = None
steps = 0
target = None
try:
    with torch.inference_mode():
        while True:
            tick = time.monotonic()
            if select.select([sys.stdin],[],[],0)[0]:
                key = sys.stdin.read(1).lower()
                if key == 'e': robot._estop_now()
                if key == 'q':
                    robot.disconnect()
                    break
                if key == 'r' and paused:
                    policy.reset()
                    paused = False
                    target = None
                    started = time.monotonic()
                    last_log = -1
                    steps = 0
                    print(f'RESUMED at up to {args.hz:g} Hz',flush=True)
                elif key != 'r':
                    pause()
            obs = robot.get_observation()
            if args.duration and time.monotonic()-started >= args.duration:
                pause()
            if paused:
                if target is None:
                    target = {k:obs[k] for k in names}
                    print(f'PAUSED after {steps} actions. Holding pose; R resumes.',flush=True)
                robot.send_action(target)
                time.sleep(.02)
                continue
            for reader in robot._readers.values():
                age = (time.time_ns()-int(reader.data['timestamp'].astype('int64')))/1e9
                if not reader.readable or not 0 <= age < .5:
                    raise RuntimeError(f'Stale observation: {age:.3f}s')
            action = policy.select_action(batch(obs))[0]*stats['action_std']+stats['action_mean']
            action = action.cpu().numpy()
            if not np.isfinite(action).all(): raise RuntimeError('Nonfinite policy action')
            current = np.array([obs[k] for k in names])
            limit = np.array([1,2,2,2,2,2,2,5]*2)
            action = np.clip(action,current-limit,current+limit)
            action = np.clip(action,[-100]*7+[0]+[-100]*7+[0],[100]*16)
            if paused or (args.duration and time.monotonic()-started >= args.duration):
                pause()
                continue
            last_sent = dict(zip(names,action))
            robot.send_action(last_sent)
            steps += 1
            second = int(time.monotonic()-started)
            if second != last_log:
                print(json.dumps({'second':second,'steps':steps,'state':current.tolist(),'sent':action.tolist()}),flush=True)
                for camera in cams:
                    (root/f'live-{camera}.jpg').write_bytes(obs[camera])
                last_log = second
            time.sleep(max(0,1/args.hz-(time.monotonic()-tick)))
except Exception as exc:
    print('PAUSING:',repr(exc),flush=True)
finally:
    if sys.exc_info()[0] is SystemExit or not robot._writers:
        termios.tcsetattr(sys.stdin,termios.TCSADRAIN,settings)
        sys.exit()
    observation = robot.get_observation()
    target = {k:observation[k] for k in names}
    print(f'PAUSED after {steps} actions. Holding current pose. Q parks, E cuts torque.',flush=True)
    while True:
        if select.select([sys.stdin],[],[],0)[0]:
            key = sys.stdin.read(1).lower()
            if key == 'e': robot._estop_now()
            if key == 'q':
                termios.tcsetattr(sys.stdin,termios.TCSADRAIN,settings)
                robot.disconnect()
                break
        robot.send_action(target)
        time.sleep(.02)
