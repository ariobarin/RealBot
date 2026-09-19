# /// script
# requires-python = "==3.10.*"
# dependencies = ["bbos", "numpy"]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""Print a live rms/peak meter from mic.audio."""
import numpy as np

from bbos import Reader

FLOOR_DB = -60.0
WIDTH = 32


def dbfs(amplitude):
    return 20.0 * np.log10(max(amplitude, 1e-9) / 32768.0)


def bar(db):
    filled = int(WIDTH * min(1.0, max(0.0, (db - FLOOR_DB) / -FLOOR_DB)))
    return "#" * filled + "-" * (WIDTH - filled)


def main():
    ref = float("nan")
    with Reader("mic.audio") as mic, Reader("mic.ref_level", keeptime=False) as level:
        while True:
            # ref is the playback echo reference: robot echo can only exist while it is hot.
            if level.ready():
                ref = float(level.data["dbfs"])
            if mic.ready():
                audio = mic.data["audio"].astype(np.float32)
                rms = dbfs(float(np.sqrt(np.mean(audio ** 2))))
                peak = dbfs(float(np.max(np.abs(audio))))
                print(f"\r[{bar(rms)}] rms {rms:6.1f}  peak {peak:6.1f}  ref {ref:6.1f}",
                      end="", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
