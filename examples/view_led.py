# /// script
# requires-python = "==3.10.*"
# dependencies = ["bbos", "numpy"]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""Drive led.ctrl through COLOURS and print led.state as it changes."""
import time
from datetime import datetime

import numpy as np

from bbos import Reader, Type, Writer

COLOURS = [
    (255, 0, 0),
    (0, 255, 0),
    (0, 0, 255),
    (255, 255, 0),
    (0, 255, 255),
    (255, 0, 255),
    (255, 255, 255),
]
HOLD_S = 2.0


def main():
    index, last = 0, None
    start = time.monotonic()
    # The daemon drops back to idle 3 s after the last write, so rewrite every iteration.
    with Writer("led.ctrl", Type("led_ctrl"), keeptime=False) as w, Reader("led.state") as state:
        while index < len(COLOURS):
            with w.buf() as b:
                b["rgb"] = np.array(COLOURS[index], dtype=np.uint8)
                b["brightness"] = np.int16(-1)
                b["period_ms"] = np.uint16(0)
            if state.ready():
                rgb = [int(v) for v in state.data["rgb"]]
                if rgb != last:
                    last = rgb
                    print(f"{datetime.now():%H:%M:%S} {rgb}")
            now = time.monotonic()
            if now - start >= HOLD_S:
                start, index = now, index + 1


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
