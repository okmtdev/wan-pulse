"""Writer test: round-trips a synthetic segment through the .wav writer."""

from __future__ import annotations

import math

import numpy as np
import soundfile as sf

from wan_pulse.gate import Segment
from wan_pulse.writer import SegmentWriter, _peak_tag


def test_write_creates_dated_wav_with_peak_in_name(tmp_path):
    audio = (np.random.default_rng(0).standard_normal(16_000) * 0.2).astype(np.float32)
    seg = Segment(
        audio=audio,
        samplerate=16_000,
        channels=1,
        start_time=1_700_000_000.0,  # fixed epoch -> deterministic folder/name
        peak_dbfs=-12.0,
    )
    writer = SegmentWriter(tmp_path)
    path = writer.write(seg)

    assert path.exists()
    assert path.suffix == ".wav"
    assert path.parent.name.count("-") == 2  # YYYY-MM-DD folder
    # The peak level is embedded so the filename doubles as a tuning meter.
    assert "peak-12.0dBFS" in path.name

    data, sr = sf.read(path)
    assert sr == 16_000
    assert len(data) == len(audio)


def test_peak_tag_handles_non_finite():
    assert _peak_tag(-math.inf) == "peakNA"
    assert _peak_tag(-12.34) == "peak-12.3dBFS"
