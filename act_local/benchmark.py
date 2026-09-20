import json
import time
import sys
import numpy as np
import torch
from lerobot.configs.types import FeatureType, PolicyFeature
from lerobot.policies.act.configuration_act import ACTConfig
from lerobot.policies.act.modeling_act import ACTPolicy

torch.set_num_threads(2)
torch.manual_seed(7)
images = ['observation.images.' + x for x in ('head', 'arm_left', 'arm_right')]
config = ACTConfig(device='cuda', chunk_size=20, n_action_steps=5,
    pretrained_backbone_weights='ResNet18_Weights.IMAGENET1K_V1' if '--real' in sys.argv else None,
    input_features={'observation.state': PolicyFeature(FeatureType.STATE, (16,)),
                    **{k: PolicyFeature(FeatureType.VISUAL, (3, 224, 224)) for k in images}},
    output_features={'action': PolicyFeature(FeatureType.ACTION, (16,))})
policy = ACTPolicy(config).cuda()
optimizer = torch.optim.AdamW(policy.get_optim_params(), lr=1e-5)
batch = {'observation.state': torch.randn(1, 16, device='cuda'),
         'action': torch.randn(1, 20, 16, device='cuda'),
         'action_is_pad': torch.zeros(1, 20, dtype=torch.bool, device='cuda'),
         **{k: torch.randn(1, 3, 224, 224, device='cuda') for k in images}}
if '--real' in sys.argv:
    with np.load('/home/bracketbot/act-local/prepared/ep000000_6d9e1e19_0.npz') as z:
        for dest, key, values in [('observation.state', 'state', slice(50, 51)),
                                  ('action', 'action', slice(50, 70))]:
            normalized = (z[key] - z[key].mean(0)) / z[key].std(0).clip(.001)
            tensor = torch.tensor(normalized[values], device='cuda')
            batch[dest] = tensor[None] if key == 'action' else tensor
        for k in images:
            tensor = torch.tensor(z[k.split('.')[-1]][50:51], device='cuda').permute(0,3,1,2).float()/255
            batch[k] = (tensor - torch.tensor([.485,.456,.406], device='cuda')[None,:,None,None]) / torch.tensor([.229,.224,.225], device='cuda')[None,:,None,None]
timings = []
for step in range(50 if '--real' in sys.argv else 12):
    start = time.monotonic()
    policy.train()
    optimizer.zero_grad(set_to_none=True)
    loss, metrics = policy(batch)
    loss.backward()
    torch.nn.utils.clip_grad_norm_(policy.parameters(), 10)
    optimizer.step()
    torch.cuda.synchronize()
    timings.append(time.monotonic() - start)
    print(json.dumps({'step': step, 'seconds': timings[-1], **metrics}), flush=True)
policy.eval()
with torch.inference_mode():
    start = time.monotonic()
    for _ in range(10):
        result = policy.predict_action_chunk(batch)
    torch.cuda.synchronize()
print(json.dumps({'synthetic_probe': '--real' not in sys.argv, 'parameters': sum(p.numel() for p in policy.parameters()),
    'train_seconds': sum(timings[2:])/len(timings[2:]),
    'inference_seconds': (time.monotonic()-start)/10,
    'peak_allocated_mb': torch.cuda.max_memory_allocated()/1e6,
    'output_shape': list(result.shape)}), flush=True)
