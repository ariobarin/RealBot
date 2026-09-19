# /// script
# requires-python = "==3.10.*"
# dependencies = ["bbos"]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""Watch the wakeword daemon: print a line whenever the wake phrase fires.

    uv run ~/bbapps/examples/view_wakeword.py

Reads ``wakeword.state`` (published at 1 Hz by the wakeword daemon) and prints
a timestamped line on every detection, plus a quiet heartbeat so you can tell
the daemon is alive.
"""
import sys
import time
from datetime import datetime

from bbos import Reader

HEARTBEAT_S = 10.0


def main():
    sys.stdout.reconfigure(line_buffering=True)
    detections = 0
    last_heartbeat = time.monotonic()

    with Reader("wakeword.state", keeptime=False) as state:
        print("[*] listening on wakeword.state (ctrl-c to quit)...")
        while True:
            if state.ready():
                if bool(state.data["active"]):
                    detections += 1
                    stamp = datetime.now().strftime("%H:%M:%S")
                    print(f"[+] {stamp} wake phrase detected! (total: {detections})")
                last_heartbeat = time.monotonic()
            elif time.monotonic() - last_heartbeat >= HEARTBEAT_S:
                print("[!] no wakeword.state updates for "
                      f"{HEARTBEAT_S:.0f}s -- is the wakeword daemon running?")
                last_heartbeat = time.monotonic()
            time.sleep(0.05)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
