# /// script
# dependencies = [
#   "bbos",
#   "soundfile",
# ]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
import sys
import numpy as np
import soundfile as sf
import time
from bbos import Writer, Reader, Config, Type

CFG = Config("speaker")

from pathlib import Path
DIR = Path(__file__).parent

name = sys.argv[1] if len(sys.argv) > 1 else "greeting"
INPUT_PATH = str(DIR / "wavs" / f"{name}.wav")

print("[+] Waiting for mic daemon...", flush=True)
with Reader("mic.audio", keeptime=False) as r:
    while not r.ready():
        time.sleep(0.1)
print("[+] Mic daemon ready", flush=True)

print(f"[+] Reading audio from {INPUT_PATH} …")
try:
    sf_reader = sf.SoundFile(INPUT_PATH, mode='r')
    print(f"[+] Sample rate: {sf_reader.samplerate}")
    print(f"[+] Channels: {sf_reader.channels}")
    print(f"[+] Duration: {len(sf_reader) / sf_reader.samplerate:.2f} seconds")

    with Writer("speaker.audio", Type("speaker_audio")) as w_speaker:
        time.sleep(0.5)  # let daemon discover the new speaker Writer
        i = 0

        start = time.monotonic()
        while True:
            input_chunk = sf_reader.read(CFG.chunk_size, dtype='int16')
            if len(input_chunk) == 0:
                break
            i += 1
            if len(input_chunk) < CFG.chunk_size:
                padding = np.zeros(CFG.chunk_size - len(input_chunk), dtype='int16')
                input_chunk = np.concatenate([input_chunk, padding])
            with w_speaker.buf() as b:
                b['audio'] = input_chunk.reshape(-1, CFG.channels)
        end = time.monotonic()
        print(f"[+] Total time: {end - start:.6f}s")
    sf_reader.close()
    print(f"[+] Played {i} chunks")
    print(f"[+] Done. Finished playing {INPUT_PATH}")
    
except FileNotFoundError:
    print(f"[-] Error: Could not find {INPUT_PATH}")
except Exception as e:
    print(f"[-] Error: {e}")
