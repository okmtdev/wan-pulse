"""Tests for the classification stage that need no TFLite backend / model.

The score -> result logic and audio coercion are pure, and the capture wiring
is exercised with a fake classifier, so this whole file runs on macOS/CI.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from wan_pulse.classify import (
    Classification,
    Classifier,
    _to_mono_16k,
    load_labels,
    scores_to_classification,
)


# A tiny AudioSet-like label set; indices line up with the score vectors below.
LABELS = ["Speech", "Dog", "Bark", "Cat", "Silence", "Growling"]


def test_to_mono_16k_downmixes_and_keeps_16k():
    stereo = np.ones((100, 2), dtype=np.float32)
    out = _to_mono_16k(stereo, 16_000)
    assert out.ndim == 1 and out.shape[0] == 100


def test_to_mono_16k_resamples():
    # 48 kHz -> 16 kHz should shrink length ~3x.
    sig = np.zeros(48_000, dtype=np.float32)
    out = _to_mono_16k(sig, 48_000)
    assert abs(out.shape[0] - 16_000) <= 1


def test_scores_pick_top_and_dog():
    scores = np.array([0.1, 0.2, 0.7, 0.05, 0.0, 0.15])  # Bark highest
    c = scores_to_classification(scores, LABELS, top_k=3, dog_threshold=0.3)
    assert c.top_label == "Bark" and c.top_score == pytest.approx(0.7)
    assert c.is_dog is True
    assert c.dog_label == "Bark"  # highest-scoring dog class
    assert len(c.top_k) == 3


def test_scores_not_dog_when_below_threshold():
    scores = np.array([0.9, 0.1, 0.05, 0.0, 0.0, 0.0])  # Speech highest, dog low
    c = scores_to_classification(scores, LABELS, top_k=5, dog_threshold=0.3)
    assert c.top_label == "Speech"
    assert c.is_dog is False
    assert c.dog_label == "Dog"  # still reports the best dog class...
    assert c.dog_score == pytest.approx(0.1)  # ...but it's under threshold


def test_classification_to_dict_and_summary():
    c = Classification("Bark", 0.82, True, "Bark", 0.82, [("Bark", 0.82)], "tflite")
    d = c.to_dict()
    assert d["is_dog"] is True and d["top_label"] == "Bark"
    assert json.dumps(d)  # must be JSON-serialisable
    assert "Bark" in c.summary()


def test_load_labels(tmp_path):
    csv_path = tmp_path / "map.csv"
    csv_path.write_text(
        "index,mid,display_name\n0,/m/09x0r,Speech\n1,/m/0bt9lr,Dog\n",
        encoding="utf-8",
    )
    assert load_labels(csv_path) == ["Speech", "Dog"]


# --- capture integration with a fake classifier ---

class FakeClassifier(Classifier):
    backend = "fake"

    def classify(self, audio, samplerate):
        return Classification("Bark", 0.9, True, "Bark", 0.9, [("Bark", 0.9)], "fake")


def test_capture_writes_sidecar_when_classifying(tmp_path):
    # Imported here so the rest of the file doesn't require sounddevice/soundfile.
    from wan_pulse.capture import Capture
    from wan_pulse.config import CaptureConfig
    from wan_pulse.gate import Segment

    config = CaptureConfig(output_dir=str(tmp_path))
    cap = Capture(config, classifier=FakeClassifier())

    seg = Segment(
        audio=np.zeros(16_000, dtype=np.float32),
        samplerate=16_000,
        channels=1,
        start_time=1_700_000_000.0,
        peak_dbfs=-12.0,
    )
    cap._emit(seg)

    wavs = list(tmp_path.rglob("*.wav"))
    sidecars = list(tmp_path.rglob("*.json"))
    assert len(wavs) == 1 and len(sidecars) == 1
    data = json.loads(sidecars[0].read_text())
    assert data["is_dog"] is True
    assert data["top_label"] == "Bark"
    assert data["peak_dbfs"] == -12.0
