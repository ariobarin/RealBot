# /// script
# requires-python = "==3.10.*"
# dependencies = ["bbos", "numpy"]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""Play CHORDS on speaker.audio as summed sine tones, HITS strikes each."""
import numpy as np

from bbos import Config, Type, Writer

CFG = Config("speaker")
AMPLITUDE = 0.25
HIT_S = 0.3   # seconds per strike, a whole number of chunks
HITS = 2
CHORDS = [
    ("Dmaj7", (294, 370, 440, 554)),
    ("D7",    (294, 370, 440, 523)),
    ("G",     (196, 247, 294)),
    ("Gm6",   (196, 233, 294, 330)),
    ("Bm",    (247, 294, 370)),
    ("E",     (165, 208, 247)),
    ("G",     (196, 247, 294)),
    ("Gm6",   (196, 233, 294, 330)),
]


def chord(freqs):
    t = np.arange(int(HIT_S * CFG.sample_rate)) / CFG.sample_rate
    # Divide by the voice count so a four-note chord cannot clip.
    wave = AMPLITUDE * sum(np.sin(2 * np.pi * hz * t) for hz in freqs) / len(freqs)
    edge = int(0.005 * CFG.sample_rate)
    wave[:edge] *= np.linspace(0, 1, edge)
    wave[-edge:] *= np.linspace(1, 0, edge)
    return (32767 * wave).astype(np.int16).reshape(-1, CFG.chunk_size, CFG.channels)


def main():
    with Writer("speaker.audio", Type("speaker_audio")) as w:
        for name, freqs in CHORDS:
            print(f"{name:6s} {freqs}", flush=True)
            for chunk in np.tile(chord(freqs), (HITS, 1, 1)):
                with w.buf() as b:
                    b["audio"] = chunk


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
