"""Segment classification: "is this a dog, and what kind of vocalization?".

This is the first slice of the "model inference" box. It uses **YAMNet**
(Google's AudioSet model) via **TFLite**, which is a great fit because:

- YAMNet's native input is 16 kHz mono float32 -- exactly what `capture` emits.
- The TFLite Interpreter API is identical on macOS (dev) and the Raspberry Pi
  (prod); only the backend package differs. So the *same model file and code*
  run as edge inference on both.

Heavy ML deps are imported lazily and are an optional install (`.[infer]`), so
the core capture skeleton keeps working (and testing) without them.

YAMNet does not output "emotion". It outputs AudioSet classes; we surface the
top label plus the best *dog* vocalization (Bark/Howl/Growling/Whimper/...),
which is a coarse proxy and the foundation for a later emotion stage.
"""

from __future__ import annotations

import csv
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import ClassifyConfig
from .emotion import EMOTION_BY_CLASS, Emotion, infer_emotion

YAMNET_SAMPLERATE = 16_000

# AudioSet display names that mean "a dog made this sound". Kept specific to
# vocalizations (plus the umbrella "Dog") so household noise isn't flagged.
DOG_CLASS_NAMES = frozenset(
    {"Dog", "Bark", "Yip", "Howl", "Bow-wow", "Growling", "Whimper (dog)", "Bay"}
)


@dataclass
class Classification:
    """Result of classifying one audio segment."""

    top_label: str
    top_score: float
    is_dog: bool
    dog_label: str | None
    dog_score: float
    top_k: list[tuple[str, float]] = field(default_factory=list)
    backend: str = ""
    # Rough, rule-based emotion guess (see emotion.py). None unless it's a dog.
    emotion: str | None = None
    emotion_basis: str | None = None
    emotion_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "top_label": self.top_label,
            "top_score": round(self.top_score, 4),
            "is_dog": self.is_dog,
            "dog_label": self.dog_label,
            "dog_score": round(self.dog_score, 4),
            "emotion": self.emotion,
            "emotion_basis": self.emotion_basis,
            "emotion_score": round(self.emotion_score, 4),
            "top_k": [[name, round(score, 4)] for name, score in self.top_k],
            "backend": self.backend,
        }

    def summary(self) -> str:
        tag = f"[dog:{self.dog_label} {self.dog_score:.2f}]" if self.is_dog else "[not-dog]"
        emo = f" 感情:{self.emotion}" if self.emotion else ""
        return f"{self.top_label} {self.top_score:.2f} {tag}{emo}"


class Classifier(ABC):
    """Anything that turns an audio segment into a :class:`Classification`."""

    @abstractmethod
    def classify(self, audio: np.ndarray, samplerate: int) -> Classification:
        ...


def scores_to_classification(
    scores: np.ndarray,
    labels: list[str],
    *,
    top_k: int,
    dog_threshold: float,
    backend: str = "",
) -> Classification:
    """Turn a per-class score vector into a Classification (pure, testable)."""
    scores = np.asarray(scores).reshape(-1)
    order = np.argsort(scores)[::-1]
    top = [(labels[i], float(scores[i])) for i in order[:top_k]]
    top_label, top_score = top[0]

    dog_label, dog_score = None, 0.0
    for i in order:  # highest-scoring dog class, if any
        if labels[i] in DOG_CLASS_NAMES:
            dog_label, dog_score = labels[i], float(scores[i])
            break
    is_dog = dog_score >= dog_threshold

    # Rough emotion guess from the expressive vocalization scores (dogs only).
    emo = Emotion(None, None, 0.0)
    if is_dog:
        expressive = {
            labels[i]: float(scores[i])
            for i in range(len(labels))
            if labels[i] in EMOTION_BY_CLASS
        }
        emo = infer_emotion(expressive)

    return Classification(
        top_label=top_label,
        top_score=top_score,
        is_dog=is_dog,
        dog_label=dog_label,
        dog_score=dog_score,
        top_k=top,
        backend=backend,
        emotion=emo.state,
        emotion_basis=emo.basis,
        emotion_score=emo.score,
    )


def _to_mono_16k(audio: np.ndarray, samplerate: int) -> np.ndarray:
    """Coerce audio to the mono float32 16 kHz waveform YAMNet expects."""
    wave = np.asarray(audio, dtype=np.float32)
    if wave.ndim > 1:  # (frames, channels) -> mono
        wave = wave.mean(axis=1)
    if samplerate != YAMNET_SAMPLERATE and wave.size:
        # Simple linear resample. capture is already 16 kHz, so this is a
        # safety net rather than the hot path.
        duration = wave.size / float(samplerate)
        n_out = int(round(duration * YAMNET_SAMPLERATE))
        if n_out > 0:
            src = np.linspace(0.0, 1.0, num=wave.size, endpoint=False)
            dst = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
            wave = np.interp(dst, src, wave).astype(np.float32)
    return wave


def _load_interpreter(model_path: str):
    """Load a TFLite interpreter from whatever backend is installed.

    Order of preference matches what's easiest to install per platform:
    ai-edge-litert (both), tflite_runtime (RPi), full TensorFlow (Mac).
    """
    errors: list[str] = []
    for loader in (
        lambda: __import__("ai_edge_litert.interpreter", fromlist=["Interpreter"]).Interpreter,
        lambda: __import__("tflite_runtime.interpreter", fromlist=["Interpreter"]).Interpreter,
        lambda: __import__("tensorflow", fromlist=["lite"]).lite.Interpreter,
    ):
        try:
            Interpreter = loader()
        except Exception as exc:  # noqa: BLE001 - try the next backend
            errors.append(type(exc).__name__)
            continue
        return Interpreter(model_path=str(model_path)), Interpreter.__module__
    raise ImportError(
        "No TFLite backend found. Install one, e.g. `pip install ai-edge-litert` "
        "(macOS/RPi) or `pip install tflite-runtime` (RPi). Tried: "
        + ", ".join(errors)
    )


def load_labels(labels_path: str | Path) -> list[str]:
    """Read the AudioSet class map CSV (index,mid,display_name) -> names."""
    path = Path(labels_path)
    if not path.is_file():
        raise FileNotFoundError(f"class map not found: {path}")
    names: list[str] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            names.append(row.get("display_name", "").strip())
    if not names:
        raise ValueError(f"no labels parsed from {path}")
    return names


class YamnetClassifier(Classifier):
    """YAMNet via TFLite, usable identically on macOS and Raspberry Pi."""

    def __init__(self, config: ClassifyConfig) -> None:
        self.config = config
        self.labels = load_labels(config.labels_path)
        if not Path(config.model_path).is_file():
            raise FileNotFoundError(
                f"model not found: {config.model_path} (run scripts/download-yamnet.sh)"
            )
        self._interpreter, self.backend = _load_interpreter(config.model_path)
        self._input = self._interpreter.get_input_details()[0]
        # YAMNet emits a [frames, num_classes] score tensor; find it by matching
        # the class count so we're robust to output ordering across model builds.
        self._score_out = self._pick_score_output()

    def _pick_score_output(self):
        outputs = self._interpreter.get_output_details()
        n = len(self.labels)
        for out in outputs:
            if int(out["shape"][-1]) == n:
                return out
        # Fall back to the first output if nothing matches the class count.
        return outputs[0]

    def _run(self, waveform: np.ndarray) -> np.ndarray:
        """Invoke the interpreter once on a 1-D waveform; return mean scores."""
        interp = self._interpreter
        interp.resize_tensor_input(self._input["index"], [waveform.size])
        interp.allocate_tensors()
        interp.set_tensor(self._input["index"], waveform.astype(np.float32))
        interp.invoke()
        scores = np.asarray(interp.get_tensor(self._score_out["index"]))
        if scores.ndim == 2:  # average over the per-frame scores
            scores = scores.mean(axis=0)
        return scores.reshape(-1)

    def classify(self, audio: np.ndarray, samplerate: int) -> Classification:
        waveform = _to_mono_16k(audio, samplerate)
        if waveform.size == 0:
            return Classification("(empty)", 0.0, False, None, 0.0, [], self.backend)

        # Some YAMNet TFLite builds want a fixed input length; window if so.
        want = int(self._input["shape"][-1])
        if want > 1 and waveform.size != want:
            scores = self._run_windowed(waveform, want)
        else:
            scores = self._run(waveform)

        return scores_to_classification(
            scores,
            self.labels,
            top_k=self.config.top_k,
            dog_threshold=self.config.dog_threshold,
            backend=self.backend,
        )

    def _run_windowed(self, waveform: np.ndarray, window: int) -> np.ndarray:
        chunks = []
        for start in range(0, waveform.size, window):
            chunk = waveform[start : start + window]
            if chunk.size < window:  # pad the tail
                chunk = np.pad(chunk, (0, window - chunk.size))
            chunks.append(self._run(chunk))
        return np.mean(chunks, axis=0)


def load_classifier(config: ClassifyConfig) -> Classifier:
    """Factory so callers don't depend on a concrete classifier class."""
    return YamnetClassifier(config)
