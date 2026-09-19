# /// script
# requires-python = ">=3.10"
# dependencies = ["bbos", "numpy<2"]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""Print the USB tree and the daemon's event log together, redrawing on change.

The tree is what telemetry uploads, verbatim: nothing is selected or
reformatted, so a new field appears here without touching this file. Below it
is the log the daemon keeps in shm, which holds what the tree cannot say, every
error and every version that led to the document above.
"""

import json
import shutil
import time

from bbos import Config, Reader

CFG = Config("usb")
LOG_FLOOR = 12                       # the tree is ~90 lines, so this is what you get


def ramlog(path):
    """The log is a fixed-size mmap, NUL-padded, so the first NUL ends it."""
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError:
        return []
    return blob.split(b"\x00", 1)[0].decode("utf-8", "replace").splitlines()


def render(blob, digest, lines):
    cols, rows = shutil.get_terminal_size((120, 40))
    try:
        tree = json.dumps(json.loads(blob), indent=2)
    except json.JSONDecodeError as e:
        tree = f"unparseable ({e}), raw:\n{blob}"
    # the log takes what the tree leaves, but never less than the floor: the
    # tree scrolls off the top rather than the log vanishing
    tail = max(LOG_FLOOR, rows - len(tree.splitlines()) - 6)
    print("\033[H\033[J", end="")            # redraw in place
    print(f"{time.strftime('%H:%M:%S')}   {digest}   {len(blob)} bytes\n")
    print(tree)
    print(f"\n{'-' * 20} {CFG.log_path}, last {tail} of {len(lines)} lines")
    for line in lines[-tail:]:
        print(line[:cols])                   # a tree line carries the whole blob
    print(flush=True)


def main():
    shown = None
    blob = digest = ""
    with Reader("usb.tree", keeptime=False) as r:
        while True:
            if r.ready():
                blob = bytes(r.data["json"]).rstrip(b"\x00").decode()
                digest = bytes(r.data["shape_hash"]).rstrip(b"\x00").decode()
            lines = ramlog(CFG.log_path)
            frame = (blob, tuple(lines[-40:]))   # either half changing redraws
            if blob and frame != shown:
                shown = frame
                render(blob, digest, lines)
            time.sleep(0.2)


if __name__ == "__main__":
    main()
