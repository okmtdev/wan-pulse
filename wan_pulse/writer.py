"""Persist captured segments to .wav files.

Files are written under ``<output_dir>/<YYYY-MM-DD>/`` with a timestamped name
so a long-running capture stays tidy and sortable.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import numpy as np
import soundfile as sf

from .gate import Segment


class SegmentWriter:
    """Writes :class:`Segment` objects to 16-bit PCM .wav files."""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)

    def write(self, segment: Segment) -> Path:
        ts = _dt.datetime.fromtimestamp(segment.start_time)
        day_dir = self.output_dir / ts.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)

        # Millisecond precision avoids collisions for back-to-back barks.
        name = "bark_" + ts.strftime("%Y%m%d_%H%M%S_") + f"{ts.microsecond // 1000:03d}.wav"
        path = day_dir / name

        audio = np.asarray(segment.audio, dtype=np.float32)
        # soundfile expects shape (frames,) or (frames, channels).
        sf.write(path, audio, segment.samplerate, subtype="PCM_16")
        return path
