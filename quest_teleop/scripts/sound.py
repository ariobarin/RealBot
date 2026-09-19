"""Speech out over the robot's speaker.

say("Teleop stopped") plays wavs/teleop_stopped.wav, pre-rendered at the speaker topic's own
format (16 kHz mono s16) so nothing synthesises or resamples on the robot. A worker streams it,
since playing inline would stall the teleop loop for the length of the phrase.

Regenerate on a Mac:
    say -v Samantha -r 170 -o wavs/<name>.wav --data-format=LEI16@16000 "<text>"
"""
import queue
import threading
import time
import wave
from pathlib import Path

import numpy as np
from bbos import Config, Type, Writer

CFG = Config("speaker")
WAV_DIR = Path(__file__).parent.parent / "wavs"

# The daemon forwards one chunk per loop into pacat's 100ms buffer, so writing at bare realtime
# starves it and every late write becomes a mid-word gap: hence the deep ring, PACE and lead-in.
RING_MS = 400
PACE = 0.90            # write at this fraction of realtime, so the daemon never finds an empty slot
LEAD_IN_S = 0.4

EXIT_JOIN_S = 5.0      # cap the teardown wait for the queue to drain


class Sound:
    """say() queues and returns; the worker plays one at a time and never raises into the loop."""

    def __init__(self):
        self._q = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._q.put(None)
        self._thread.join(timeout=EXIT_JOIN_S)
        return False

    def say(self, text):
        self._q.put(text)

    def _load(self, text):
        """wavs/<text>.wav, lead-in prepended and padded out to whole chunks."""
        path = WAV_DIR / f"{text.lower().replace(' ', '_')}.wav"
        with wave.open(str(path)) as f:
            fmt = (f.getframerate(), f.getnchannels(), f.getsampwidth())
            if fmt != (CFG.sample_rate, CFG.channels, 2):
                raise ValueError(f"{path.name} is {fmt}, need "
                                 f"({CFG.sample_rate}, {CFG.channels}, 2)")
            audio = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
        chunk = CFG.chunk_size
        lead = np.zeros(int(LEAD_IN_S * CFG.sample_rate), dtype=np.int16)
        tail = np.zeros(-(len(lead) + len(audio)) % chunk, dtype=np.int16)
        return np.concatenate([lead, audio, tail])

    def _run(self):
        chunk = CFG.chunk_size
        period = PACE * chunk / CFG.sample_rate
        try:
            # keeptime=False: bbos's Loop is the MAIN thread's clock. A keeptime writer here
            # would re-pace the teleop loop into it (gcd(15, 100) = 5ms) and would silently
            # drop every chunk that missed its trigger.
            with Writer("speaker.audio", Type("speaker_audio"),
                        keeptime=False, buf_ms=RING_MS) as w:
                for text in iter(self._q.get, None):
                    try:
                        audio = self._load(text)
                        due = time.monotonic()
                        for i in range(0, len(audio), chunk):
                            with w.buf() as b:
                                b["audio"] = audio[i:i + chunk].reshape(-1, CFG.channels)
                            due += period
                            time.sleep(max(0.0, due - time.monotonic()))
                    except Exception as e:
                        print(f"[sound] {text!r} failed: {e}", flush=True)
        except Exception as e:
            print(f"[sound] speech OFF: {e}", flush=True)
