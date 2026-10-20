"""Shutter sound.

Lightshot plays a small camera click on every capture.  Rather than shipping a
binary WAV we synthesise one on first use (two short noise bursts with an
exponential decay envelope) and cache it under the app data directory.
"""

from __future__ import annotations

import array
import math
import random
import shutil
import subprocess
import wave
from pathlib import Path

from .config import data_dir

SAMPLE_RATE = 44100


def shutter_path() -> Path:
    path = data_dir() / "shutter.wav"
    if not path.is_file():
        _generate(path)
    return path


def _generate(path: Path) -> None:
    rng = random.Random(20240517)
    samples: array.array = array.array("h")

    def burst(start: float, length: float, amp: float, decay: float) -> None:
        total = int(SAMPLE_RATE * length)
        offset = int(SAMPLE_RATE * start)
        while len(samples) < offset:
            samples.append(0)
        for i in range(total):
            t = i / SAMPLE_RATE
            env = math.exp(-t * decay)
            noise = rng.uniform(-1.0, 1.0)
            click = math.sin(2 * math.pi * 2600 * t) * 0.35
            value = (noise * 0.65 + click) * env * amp
            samples.append(int(max(-1.0, min(1.0, value)) * 32767))

    burst(0.000, 0.045, 0.55, 130)   # mirror-up click
    burst(0.070, 0.055, 0.42, 110)   # shutter close

    # tiny fade-out tail
    for i in range(int(SAMPLE_RATE * 0.05)):
        samples.append(0)

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(samples.tobytes())


class ClickPlayer:
    """Best-effort, non-blocking shutter playback (silently no-ops if unsupported).

    Prefers QtMultimedia; falls back to PulseAudio/PipeWire/ALSA command line
    players so the shutter still clicks on distributions that ship PyQt6 without
    the QtMultimedia module.
    """

    def __init__(self) -> None:
        self._effect = None
        self._command: list[str] | None = None
        self._path = shutter_path()

        try:
            from PyQt6.QtCore import QUrl
            from PyQt6.QtMultimedia import QSoundEffect  # type: ignore

            effect = QSoundEffect()
            effect.setSource(QUrl.fromLocalFile(str(self._path)))
            effect.setVolume(0.35)
            self._effect = effect
            return
        except Exception:
            self._effect = None

        for candidate in ("paplay", "pw-play", "aplay", "play"):
            if shutil.which(candidate):
                self._command = [candidate, str(self._path)]
                break

    def play(self) -> None:
        if self._effect is not None:
            try:
                self._effect.play()
                return
            except Exception:
                pass
        if self._command:
            try:
                subprocess.Popen(
                    self._command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception:
                pass
