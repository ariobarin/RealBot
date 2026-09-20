#!/usr/bin/env python
"""Fast path for a short time budget: freeze the ResNet18 backbone, precompute its
feature maps once, and train only the ACT transformer + VAE on them.

Equivalent to training with a frozen backbone (ACT uses FrozenBatchNorm, so eval-mode
features are exact), but ~10x cheaper per step on the Orin Nano. Checkpoints are saved
with the unchanged backbone weights, so live2.py loads them like any other run.
"""
import argparse
import json
import shutil
import time
from pathlib import Path

import numpy as np
import torch
import draccus
from lerobot.configs.policies import PreTrainedConfig
from lerobot.policies.act.modeling_act import ACTPolicy

parser = argparse.ArgumentParser()
parser.add_argument('--steps', type=int, default=30000)
parser.add_argument('--batch', type=int, default=16)
parser.add_argument('--lr', type=float, default=3e-5)
parser.add_argument('--init', default='run-v1/step-2000')
parser.add_argument('--out', default='run-v3')
parser.add_argument('--eval-every', type=int, default=500)
parser.add_argument('--minutes', type=float, default=0, help='stop after this many minutes of training (0 = all steps)')
args = parser.parse_args()

root = Path('/home/bracketbot/act-local')
out = root / args.out
out.mkdir(exist_ok=True)
cache = root / 'cache'
cameras = ('head', 'arm_left', 'arm_right')
chunk = 10
torch.manual_seed(7)
rng = np.random.default_rng(int(time.time()))
torch.set_num_threads(2)

assert (cache / 'index.json').exists(), 'run train2.py once to build cache/'
meta = json.loads((cache / 'index.json').read_text())
images = {c: np.load(cache / f'{c}.npy', mmap_mode='r') for c in cameras}
state_all = np.load(cache / 'state.npy')
action_all = np.load(cache / 'action.npy')
valid_all = np.load(cache / 'valid.npy')
n = len(valid_all)
split = json.loads((root / 'run-v1/split.json').read_text())
starts = {}
for m in meta:
    v = valid_all[m['offset']:m['offset'] + m['length']].astype(int)
    starts[m['name']] = np.flatnonzero(np.convolve(v, np.ones(chunk, int), 'valid') == chunk) + m['offset']
train_starts = np.concatenate([starts[k] for k in split['train']])
val_names = split['validation']

first = np.stack([state_all[m['offset']] for m in meta])
steps_100ms = np.concatenate([np.abs(np.diff(action_all[m['offset']:m['offset'] + m['length']], axis=0)) for m in meta])
lead = np.concatenate([np.abs(action_all[m['offset']:m['offset'] + m['length']] - state_all[m['offset']:m['offset'] + m['length']]) for m in meta])
(out / 'demo_pose.json').write_text(json.dumps({
    'start_mean': first.mean(0).round(3).tolist(), 'start_std': first.std(0).round(3).tolist(),
    'step_100ms_p99': np.percentile(steps_100ms, 99, axis=0).round(3).tolist(),
    'lead_p99': np.percentile(lead, 99, axis=0).round(3).tolist(), 'tick_hz': 10}, indent=2))
shutil.copy(root / 'run-v1/stats.npz', out / 'stats.npz')
shutil.copy(root / 'run-v1/split.json', out / 'split.json')
with np.load(root / 'run-v1/stats.npz') as z:
    stats = {k: torch.tensor(z[k], device='cuda') for k in z.files}

init = root / args.init
config = draccus.decode(PreTrainedConfig, {'type': 'act', **json.loads((init / 'config.json').read_text())})
policy = ACTPolicy.from_pretrained(init, config=config, strict=True).cuda()
image_mean = torch.tensor([.485, .456, .406], device='cuda').view(1, 3, 1, 1)
image_std = torch.tensor([.229, .224, .225], device='cuda').view(1, 3, 1, 1)

# ------------------------------------------------ precompute frozen backbone features
stamp = cache / 'feat_init.txt'
if not (stamp.exists() and stamp.read_text() == args.init and all((cache / f'feat_{c}.npy').exists() for c in cameras)):
    print('precomputing backbone features from', init, flush=True)
    backbone = policy.model.backbone.eval()
    t0 = time.monotonic()
    for c in cameras:
        mm = None
        for i in range(0, n, 32):
            im = torch.from_numpy(np.ascontiguousarray(images[c][i:i + 32])).cuda().permute(0, 3, 1, 2).float() / 255
            with torch.inference_mode(), torch.autocast('cuda', dtype=torch.bfloat16):
                f = backbone((im - image_mean) / image_std)['feature_map'].float().cpu().numpy().astype(np.float16)
            if mm is None:
                mm = np.lib.format.open_memmap(cache / f'feat_{c}.npy', 'w+', np.float16, (n, *f.shape[1:]))
            mm[i:i + len(f)] = f
        mm.flush()
        print(json.dumps({'features': c, 'shape': list(mm.shape), 'seconds': round(time.monotonic() - t0, 1)}), flush=True)
    stamp.write_text(args.init)
feats = {c: np.load(cache / f'feat_{c}.npy', mmap_mode='r') for c in cameras}


class Passthrough(torch.nn.Module):
    def forward(self, x):
        return {'feature_map': x}


real_backbone = policy.model.backbone
passthrough = Passthrough()
policy.model.backbone = passthrough
optimizer = torch.optim.AdamW(policy.parameters(), lr=args.lr, weight_decay=config.optimizer_weight_decay)
amp = torch.autocast('cuda', dtype=torch.bfloat16)


def make_batch(idx):
    idx = np.sort(np.asarray(idx))
    st = torch.from_numpy(state_all[idx]).cuda()
    ac = torch.from_numpy(np.stack([action_all[i:i + chunk] for i in idx])).cuda()
    b = {'observation.state': (st - stats['state_mean']) / stats['state_std'],
         'action': (ac - stats['action_mean']) / stats['action_std'],
         'action_is_pad': torch.zeros(len(idx), chunk, dtype=torch.bool, device='cuda')}
    for c in cameras:
        b['observation.images.' + c] = torch.from_numpy(np.stack([feats[c][i] for i in idx])).cuda().float()
    return b


@torch.inference_mode()
def evaluate():
    policy.eval()
    idx = []
    for name in val_names:
        s = starts[name]
        idx.extend(s[np.linspace(0, len(s) - 1, 5).astype(int)].tolist())
    errors, baselines = [], []
    for i in range(0, len(idx), args.batch):
        b = make_batch(idx[i:i + args.batch])
        with amp:
            predicted = policy.predict_action_chunk(b).float()
        errors.append((predicted - b['action']).abs().mean(dim=(1, 2)))
        hold = (b['observation.state'] * stats['state_std'] + stats['state_mean'] - stats['action_mean']) / stats['action_std']
        baselines.append((hold[:, None] - b['action']).abs().mean(dim=(1, 2)))
    return {'validation_l1': torch.cat(errors).mean().item(), 'hold_position_l1': torch.cat(baselines).mean().item()}


def save(tag, step, metrics):
    target = out / tag
    if target.exists():
        shutil.rmtree(target)
    policy.model.backbone = real_backbone          # checkpoint carries the real (frozen) backbone
    policy.save_pretrained(target)
    policy.model.backbone = passthrough
    (target / 'metrics.json').write_text(json.dumps({'step': step, **metrics}))


log = (out / 'log.jsonl').open('a')
def emit(record):
    line = json.dumps(record)
    print(line, flush=True)
    log.write(line + '\n')
    log.flush()

metrics = evaluate()
emit({'step': 0, 'init': str(init), 'batch': args.batch, 'lr': args.lr, **metrics})
best = metrics['validation_l1']
started = time.monotonic()
step = 0
while step < args.steps:
    step += 1
    b = make_batch(rng.choice(train_starts, size=args.batch, replace=False))
    policy.train()
    optimizer.zero_grad(set_to_none=True)
    with amp:
        loss, info = policy(b)
    assert torch.isfinite(loss), 'non-finite loss'
    loss.backward()
    torch.nn.utils.clip_grad_norm_(policy.parameters(), 10)
    optimizer.step()
    elapsed = time.monotonic() - started
    out_of_time = args.minutes and elapsed >= args.minutes * 60
    if step % 100 == 0 or step == 1:
        emit({'step': step, 'elapsed_seconds': round(elapsed, 1), 'steps_per_second': round(step / elapsed, 2),
              'l1_loss': float(info['l1_loss']), 'kld_loss': float(info['kld_loss']),
              'peak_mb': round(torch.cuda.max_memory_allocated() / 1e6)})
    if step % args.eval_every == 0 or step == args.steps or out_of_time:
        metrics = evaluate()
        emit({'step': step, **metrics, 'best_so_far': min(best, metrics['validation_l1'])})
        save('last', step, metrics)
        if metrics['validation_l1'] < best:
            best = metrics['validation_l1']
            save('best', step, metrics)
    if out_of_time:
        break
emit({'finished': True, 'steps': step, 'best_validation_l1': best, 'elapsed_seconds': round(time.monotonic() - started, 1)})
