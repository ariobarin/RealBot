#!/usr/bin/env python
"""Continue training the electrical-box ACT policy from run-v1 with a real batch size.

Changes vs train.py:
  * batch 8 (was 1), bf16 autocast, gradient clipping kept at 10
  * data served from a flat memory-mapped cache (cache/*.npy) instead of
    re-reading a ~25 MB .npz episode every step
  * resumes from run-v1/step-2000 weights, keeps run-v1 normalization stats
  * evaluates every --eval-every steps against the hold-position baseline and
    keeps <out>/best (lowest validation L1) plus <out>/last
  * writes <out>/demo_pose.json (demo start pose + per-tick step sizes) for live2.py
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
parser.add_argument('--steps', type=int, default=20000)
parser.add_argument('--batch', type=int, default=8)
parser.add_argument('--lr', type=float, default=1e-5)
parser.add_argument('--init', default='run-v1/step-2000', help='checkpoint dir to resume weights from')
parser.add_argument('--out', default='run-v2')
parser.add_argument('--eval-every', type=int, default=500)
parser.add_argument('--no-amp', action='store_true')
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
torch.backends.cudnn.benchmark = True

# ---------------------------------------------------------------- flat cache
paths = sorted((root / 'prepared').glob('*.npz'))
split = json.loads((root / 'run-v1/split.json').read_text())
if not (cache / 'index.json').exists():
    print('building flat cache in', cache, flush=True)
    cache.mkdir(exist_ok=True)
    n, meta = 0, []
    for path in paths:
        with np.load(path) as z:
            t = len(z['valid'])
        meta.append({'name': path.name, 'offset': n, 'length': t})
        n += t
    imgs = {c: np.lib.format.open_memmap(cache / f'{c}.npy', 'w+', np.uint8, (n, 224, 224, 3)) for c in cameras}
    st = np.lib.format.open_memmap(cache / 'state.npy', 'w+', np.float32, (n, 16))
    ac = np.lib.format.open_memmap(cache / 'action.npy', 'w+', np.float32, (n, 16))
    valid = np.zeros(n, bool)
    for m, path in zip(meta, paths):
        with np.load(path) as z:
            s = slice(m['offset'], m['offset'] + m['length'])
            for c in cameras:
                imgs[c][s] = z[c]
            st[s] = z['state']
            ac[s] = z['action']
            valid[s] = z['valid']
    for a in (*imgs.values(), st, ac):
        a.flush()
    np.save(cache / 'valid.npy', valid)
    (cache / 'index.json').write_text(json.dumps(meta))
    del imgs, st, ac
meta = json.loads((cache / 'index.json').read_text())
images = {c: np.load(cache / f'{c}.npy', mmap_mode='r') for c in cameras}
state_all = np.load(cache / 'state.npy')
action_all = np.load(cache / 'action.npy')
valid_all = np.load(cache / 'valid.npy')

starts = {}
for m in meta:
    v = valid_all[m['offset']:m['offset'] + m['length']].astype(int)
    ok = np.flatnonzero(np.convolve(v, np.ones(chunk, int), 'valid') == chunk)
    starts[m['name']] = ok + m['offset']
train_starts = np.concatenate([starts[n] for n in split['train']])
val_names = split['validation']
print(json.dumps({'episodes': len(meta), 'frames': int(len(valid_all)), 'train_chunks': int(len(train_starts)),
                  'validation_episodes': len(val_names)}), flush=True)

# demo pose summary for live2.py (start pose, per-100ms step sizes)
first = np.stack([state_all[m['offset']] for m in meta])
steps_100ms = np.concatenate([np.abs(np.diff(action_all[m['offset']:m['offset'] + m['length']], axis=0)) for m in meta])
lead = np.concatenate([np.abs(action_all[m['offset']:m['offset'] + m['length']] - state_all[m['offset']:m['offset'] + m['length']]) for m in meta])
(out / 'demo_pose.json').write_text(json.dumps({
    'start_mean': first.mean(0).round(3).tolist(), 'start_std': first.std(0).round(3).tolist(),
    'step_100ms_p99': np.percentile(steps_100ms, 99, axis=0).round(3).tolist(),
    'lead_p99': np.percentile(lead, 99, axis=0).round(3).tolist(), 'tick_hz': 10}, indent=2))

# ---------------------------------------------------------------- model
with np.load(root / 'run-v1/stats.npz') as z:
    stats = {k: torch.tensor(z[k], device='cuda') for k in z.files}
shutil.copy(root / 'run-v1/stats.npz', out / 'stats.npz')
shutil.copy(root / 'run-v1/split.json', out / 'split.json')
init = root / args.init
config = draccus.decode(PreTrainedConfig, {'type': 'act', **json.loads((init / 'config.json').read_text())})
config.optimizer_lr = args.lr
config.optimizer_lr_backbone = args.lr
policy = ACTPolicy.from_pretrained(init, config=config, strict=True).cuda()
optimizer = torch.optim.AdamW(policy.get_optim_params(), lr=args.lr, weight_decay=config.optimizer_weight_decay)
image_mean = torch.tensor([.485, .456, .406], device='cuda').view(1, 3, 1, 1)
image_std = torch.tensor([.229, .224, .225], device='cuda').view(1, 3, 1, 1)
amp = torch.autocast('cuda', dtype=torch.bfloat16, enabled=not args.no_amp)


def make_batch(idx):
    idx = np.sort(np.asarray(idx))
    st = torch.from_numpy(state_all[idx]).cuda()
    ac = torch.from_numpy(np.stack([action_all[i:i + chunk] for i in idx])).cuda()
    b = {'observation.state': (st - stats['state_mean']) / stats['state_std'],
         'action': (ac - stats['action_mean']) / stats['action_std'],
         'action_is_pad': torch.zeros(len(idx), chunk, dtype=torch.bool, device='cuda')}
    for c in cameras:
        im = torch.from_numpy(np.stack([images[c][i] for i in idx])).cuda().permute(0, 3, 1, 2).float() / 255
        b['observation.images.' + c] = (im - image_mean) / image_std
    return b


@torch.inference_mode()
def evaluate():
    policy.eval()
    idx = []
    for name in val_names:
        s = starts[name]
        idx.extend(s[np.linspace(0, len(s) - 1, 3).astype(int)].tolist())
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
    policy.save_pretrained(target)
    (target / 'metrics.json').write_text(json.dumps({'step': step, **metrics}))


log = (out / 'log.jsonl').open('a')
def emit(record):
    line = json.dumps(record)
    print(line, flush=True)
    log.write(line + '\n')
    log.flush()

metrics = evaluate()
emit({'step': 0, 'init': str(init), **metrics})
best = metrics['validation_l1']
started = time.monotonic()
for step in range(1, args.steps + 1):
    b = make_batch(rng.choice(train_starts, size=args.batch, replace=False))
    policy.train()
    optimizer.zero_grad(set_to_none=True)
    with amp:
        loss, info = policy(b)
    assert torch.isfinite(loss), 'non-finite loss'
    loss.backward()
    torch.nn.utils.clip_grad_norm_(policy.parameters(), 10)
    optimizer.step()
    if step % 50 == 0 or step == 1:
        elapsed = time.monotonic() - started
        emit({'step': step, 'elapsed_seconds': round(elapsed, 1), 'eta_minutes': round((args.steps - step) * elapsed / step / 60, 1),
              'l1_loss': float(info['l1_loss']), 'kld_loss': float(info['kld_loss']),
              'peak_mb': round(torch.cuda.max_memory_allocated() / 1e6)})
    if step % args.eval_every == 0 or step == args.steps:
        metrics = evaluate()
        emit({'step': step, **metrics, 'best_so_far': min(best, metrics['validation_l1'])})
        save('last', step, metrics)
        if metrics['validation_l1'] < best:
            best = metrics['validation_l1']
            save('best', step, metrics)
emit({'finished': True, 'steps': args.steps, 'best_validation_l1': best, 'elapsed_seconds': round(time.monotonic() - started, 1)})
