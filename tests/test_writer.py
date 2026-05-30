"""Writer test: round-trips a synthetic segment through the .wav writer."""

from __future__ import annotations

import numpy as np
import soundfile as sf

from wan_pulse.gate import Segment
from wan_pulse.writer import SegmentWriter


def test_write_creates_dated_wav(tmp_path):
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

    data, sr = sf.read(path)
    assert sr == 16_000
    assert len(data) == len(audio)
