"""Persist captured segments to .wav files.

Files are written under ``<output_dir>/<YYYY-MM-DD>/`` with a name that carries
both the timestamp and the segment's **peak level (dBFS)**. The peak is the most
useful number for tuning: start with a low threshold so nothing is missed, then
look at the peaks in the filenames to decide where to set the threshold.

Example: ``bark_20260530_071530_812_peak-11.3dBFS.wav``
"""

from __future__ import annotations

import datetime as _dt
import math
from pathlib import Path

import numpy as np
import soundfile as sf

from .gate import Segment


def _peak_tag(peak_dbfs: float) -> str:
    """Filename-safe representation of the peak level."""
    if not math.isfinite(peak_dbfs):
        return "peakNA"
    return f"peak{peak_dbfs:.1f}dBFS"


class SegmentWriter:
    """Writes :class:`Segment` objects to 16-bit PCM .wav files."""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)

    def write(self, segment: Segment) -> Path:
        ts = _dt.datetime.fromtimestamp(segment.start_time)
        day_dir = self.output_dir / ts.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)

        # Millisecond precision avoids collisions for back-to-back barks; the
        # peak tag turns the filename into a tuning meter.
        stamp = ts.strftime("%Y%m%d_%H%M%S_") + f"{ts.microsecond // 1000:03d}"
        name = f"bark_{stamp}_{_peak_tag(segment.peak_dbfs)}.wav"
        path = day_dir / name

        audio = np.asarray(segment.audio, dtype=np.float32)
        # soundfile expects shape (frames,) or (frames, channels).
        sf.write(path, audio, segment.samplerate, subtype="PCM_16")
        return path

