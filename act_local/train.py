import argparse
from functools import lru_cache
import json
from pathlib import Path
import time

import numpy as np
import torch
from lerobot.configs.types import FeatureType, PolicyFeature
from lerobot.policies.act.configuration_act import ACTConfig
from lerobot.policies.act.modeling_act import ACTPolicy

parser = argparse.ArgumentParser()
parser.add_argument('--steps', type=int, default=2000)
args = parser.parse_args()
root = Path('/home/bracketbot/act-local')
output = root / 'run-v1'
output.mkdir(exist_ok=False)
torch.set_num_threads(2)
torch.manual_seed(7)
rng = np.random.default_rng(7)
paths = sorted((root / 'prepared').glob('*.npz'))
assert len(paths) == 87, len(paths)
validation = paths[::10]
training = [p for p in paths if p not in validation]
chunk = 10
cameras = ('head', 'arm_left', 'arm_right')
starts, states, actions = {}, [], []
for path in paths:
    with np.load(path) as z:
        starts[path] = np.flatnonzero(np.convolve(z['valid'].astype(int), np.ones(chunk, int), 'valid') == chunk)
        if path in training:
            states.append(z['state'][z['valid']])
            actions.append(z['action'][z['valid']])
assert all(len(starts[p]) for p in paths)
stats = {}
for name, values in [('state', states), ('action', actions)]:
    values = np.concatenate(values)
    stats[name + '_mean'] = torch.tensor(values.mean(0), device='cuda')
    stats[name + '_std'] = torch.tensor(values.std(0).clip(.1), device='cuda')
np.savez(output / 'stats.npz', **{k: v.cpu().numpy() for k, v in stats.items()})
(output / 'split.json').write_text(json.dumps({'train': [p.name for p in training],
    'validation': [p.name for p in validation], 'seed': 7}, indent=2))
config = ACTConfig(device='cuda', chunk_size=chunk, n_action_steps=5,
    input_features={'observation.state': PolicyFeature(FeatureType.STATE, (16,)),
        **{'observation.images.' + c: PolicyFeature(FeatureType.VISUAL, (3, 224, 224)) for c in cameras}},
    output_features={'action': PolicyFeature(FeatureType.ACTION, (16,))})
policy = ACTPolicy(config).cuda()
optimizer = torch.optim.AdamW(policy.get_optim_params(), lr=1e-5)
image_mean = torch.tensor([.485, .456, .406], device='cuda').view(1, 3, 1, 1)
image_std = torch.tensor([.229, .224, .225], device='cuda').view(1, 3, 1, 1)

@lru_cache(maxsize=2)
def episode(path):
    with np.load(path) as z:
        return {k: z[k] for k in ('state', 'action', *cameras)}

def batch(path, index):
    data = episode(path)
    state = torch.tensor(data['state'][index:index+1], device='cuda')
    action = torch.tensor(data['action'][index:index+chunk][None], device='cuda')
    result = {'observation.state': (state - stats['state_mean']) / stats['state_std'],
        'action': (action - stats['action_mean']) / stats['action_std'],
        'action_is_pad': torch.zeros(1, chunk, dtype=torch.bool, device='cuda')}
    for c in cameras:
        image = torch.tensor(data[c][index:index+1], device='cuda').permute(0, 3, 1, 2).float() / 255
        result['observation.images.' + c] = (image - image_mean) / image_std
    return result

@torch.inference_mode()
def evaluate():
    errors, baselines = [], []
    for path in validation:
        for index in starts[path][np.linspace(0, len(starts[path])-1, 3).astype(int)]:
            b = batch(path, int(index))
            predicted = policy.predict_action_chunk(b)
            errors.append((predicted - b['action']).abs().mean().item())
            hold = (b['observation.state'] * stats['state_std'] + stats['state_mean'] - stats['action_mean']) / stats['action_std']
            baselines.append((hold[:, None] - b['action']).abs().mean().item())
    return {'validation_l1': float(np.mean(errors)), 'hold_position_l1': float(np.mean(baselines))}

print(json.dumps({'step': 0, **evaluate()}), flush=True)
started = time.monotonic()
for step in range(1, args.steps + 1):
    path = training[int(rng.integers(len(training)))]
    b = batch(path, int(rng.choice(starts[path])))
    policy.train()
    optimizer.zero_grad(set_to_none=True)
    loss, metrics = policy(b)
    assert torch.isfinite(loss)
    loss.backward()
    torch.nn.utils.clip_grad_norm_(policy.parameters(), 10)
    optimizer.step()
    if step % 25 == 0:
        print(json.dumps({'step': step, 'elapsed_seconds': time.monotonic()-started, **metrics}), flush=True)
    if step % 250 == 0 or step == args.steps:
        print(json.dumps({'step': step, **evaluate()}), flush=True)
        policy.save_pretrained(output / f'step-{step}')
        torch.save({'optimizer': optimizer.state_dict(), 'step': step}, output / 'optimizer.pt')
print(json.dumps({'finished': True, 'steps': args.steps, 'elapsed_seconds': time.monotonic()-started}), flush=True)
