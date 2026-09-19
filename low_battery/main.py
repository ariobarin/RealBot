# /// script
# requires-python = "==3.10.*"
# dependencies = ["bbos", "numpy"]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""Shout "Low battery" over whatever holds the speaker, whenever drive.status is under base.low_battery_v."""
import time
import wave
from pathlib import Path

import numpy as np

from bbos import Config, Reader, Type, Writer

CFG = Config("speaker")
BASE = Config("base")
WAV = Path(__file__).parent / "wavs" / "low_battery.wav"

ALERT_S = 15.0

# The daemon forwards one chunk per loop into pacat's 100ms buffer, so bare realtime starves it.
RING_MS = 400
PACE = 0.90
LEAD_IN_S = 0.4


def load():
    with wave.open(str(WAV)) as f:
        fmt = (f.getframerate(), f.getnchannels(), f.getsampwidth())
        if fmt != (CFG.sample_rate, CFG.channels, 2):
            raise ValueError(f"{WAV.name} is {fmt}, need "
                             f"({CFG.sample_rate}, {CFG.channels}, 2)")
        audio = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
    lead = np.zeros(int(LEAD_IN_S * CFG.sample_rate), dtype=np.int16)
    tail = np.zeros(-(len(lead) + len(audio)) % CFG.chunk_size, dtype=np.int16)
    return np.concatenate([lead, audio, tail])


def shout(audio):
    """Unlinking orphans the incumbent's segment; the daemon's Reader follows the new inode."""
    Path("/dev/shm/speaker.audio").unlink(missing_ok=True)
    with Writer("speaker.audio", Type("speaker_audio"),
                keeptime=False, buf_ms=RING_MS) as w:
        due = time.monotonic()
        for i in range(0, len(audio), CFG.chunk_size):
            with w.buf() as b:
                b["audio"] = audio[i:i + CFG.chunk_size].reshape(-1, CFG.channels)
            due += PACE * CFG.chunk_size / CFG.sample_rate
            time.sleep(max(0.0, due - time.monotonic()))


def main():
    audio = load()
    print(f"[shout] watching drive.status under {BASE.low_battery_v}V", flush=True)
    last = -ALERT_S
    with Reader("drive.status") as r:
        while True:
            if r.ready():
                voltage = float(r.data["voltage"])
                if (voltage < BASE.low_battery_v
                        and time.monotonic() - last >= ALERT_S):
                    print(f"[shout] {voltage:.2f}V", flush=True)
                    shout(audio)
                    last = time.monotonic()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
