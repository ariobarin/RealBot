import sys
sys.path.insert(0, "/home/bracketbot/bbos")
from bbos import Reader
for side in ("left", "right"):
    with Reader(f"arm_{side}.ctrl", keeptime=False) as r:
        r.ready()
        print(f"arm_{side}.ctrl", "OWNED by another writer" if r.readable else "free")
