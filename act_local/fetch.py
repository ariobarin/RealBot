import concurrent.futures
import json
from pathlib import Path
import urllib.request

root = Path('/home/bracketbot/act-local')
files = json.loads((root / 'download.json').read_text())['files']
files = [f for f in files if f['key'].endswith(('.npz', '.mkv')) and '/_derived/' not in f['key']]
assert len(files) == 348, len(files)
print('Clean raw files:', len(files), 'bytes:', sum(f['size'] for f in files), flush=True)
def fetch(f):
    path = root / 'raw' / f['key'].split('open_electrical_box_0188_v2/', 1)[1]
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size != f['size']:
        temporary = path.with_suffix(path.suffix + '.part')
        urllib.request.urlretrieve(f['url'], temporary)
        assert temporary.stat().st_size == f['size']
        temporary.replace(path)
    return path.name
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    for i, name in enumerate(pool.map(fetch, files), 1):
        if i % 20 == 0 or i == len(files):
            print(i, '/', len(files), name, flush=True)
