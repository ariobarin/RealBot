import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import av
import numpy as np
from PIL import Image

root = Path('/home/bracketbot/act-local')
out = root / 'prepared'
out.mkdir(exist_ok=True)
cams = ('head', 'arm_left', 'arm_right')
def prepare(path):
    if (out / path.name).exists():
        with np.load(out / path.name) as done:
            return {'episode': path.stem, 'ticks': len(done['valid']), 'valid': int(done['valid'].sum())}
    with np.load(path) as z:
        times = {k: z[k]['timestamp'].astype('int64') for k in
                 ('arm_left_state', 'arm_right_state', 'arm_left_ctrl', 'arm_right_ctrl')}
        times.update({c: z[f'video_{c}_timestamp_ns'] for c in cams})
        assert all(np.all(np.diff(t) >= 0) for t in times.values()), path.name
        start = max(int(t[0]) for t in times.values())
        end = min(int(t[-1]) for t in times.values())
        ticks = np.arange(start, end, 100_000_000, dtype=np.int64)
        indices = {k: np.searchsorted(t, ticks, side='right') - 1 for k, t in times.items()}
        ages = np.stack([(ticks - times[k][idx]) / 1e9 for k, idx in indices.items()])
        valid = (ages <= .25).all(axis=0)
        arrays = {'timestamp_ns': ticks, 'valid': valid}
        for dest, suffix in [('state', 'state'), ('action', 'ctrl')]:
            arms = []
            for side in ('left', 'right'):
                pos = z[f'arm_{side}_{suffix}']['pos'][indices[f'arm_{side}_{suffix}']]
                low, high = z[f'cal_arm_{side}_min'], z[f'cal_arm_{side}_max']
                assert np.all(high != low)
                frac = np.clip((pos - low) / (high - low), 0, 1)
                norm = frac * 200 - 100
                norm[:, 7] = frac[:, 7] * 100
                arms.append(norm)
            arrays[dest] = np.concatenate(arms, axis=1).astype(np.float32)
            assert np.isfinite(arrays[dest]).all(), path.name
        for camera in cams:
            frames = {}
            selected = set(indices[camera])
            with av.open(str(root / 'raw/video' / f'{camera}_{path.stem}.mkv')) as video:
                for frame_index, frame in enumerate(video.decode(video=0)):
                    if frame_index not in selected:
                        continue
                    image = frame.to_image()
                    if camera == 'head':
                        image = image.crop((0, 0, image.width // 2, image.height))
                    frames[frame_index] = np.asarray(image.resize((224, 224), Image.Resampling.BILINEAR))
            assert frame_index + 1 == len(times[camera]), (path.name, camera, frame_index + 1, len(times[camera]))
            arrays[camera] = np.stack([frames[i] for i in indices[camera]])
        arrays.update({k: z[k] for k in z.files if k.startswith('cal_')})
        temporary = out / (path.stem + '.part.npz')
        np.savez(temporary, **arrays)
        temporary.replace(out / path.name)
        summary = {'episode': path.stem, 'ticks': len(ticks), 'valid': int(valid.sum()),
                   'max_age_seconds': float(ages.max())}
        print(json.dumps(summary), flush=True)
        return summary
with ThreadPoolExecutor(max_workers=3) as pool:
    summaries = list(pool.map(prepare, sorted((root / 'raw/episodes').glob('*.npz'))))
(out / 'manifest.json').write_text(json.dumps({'fps': 10, 'cameras': cams,
    'image_size': [224, 224], 'head_crop': 'left half', 'joint_order': 'left 8 then right 8',
    'units': 'native adapter calibration ranges: joints [-100,100], grippers [0,100]',
    'alignment': 'last sample at or before tick; exclude chunks with sample age >250ms',
    'episodes': summaries}, indent=2))
