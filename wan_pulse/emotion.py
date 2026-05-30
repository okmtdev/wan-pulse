"""Rough rule-based emotion guess from a dog's vocalization type.

This is the "good enough to see it working" emotion stage: it is **not** a
trained emotion model. It maps the AudioSet vocalization class that YAMNet
detected (Bark / Growling / Howl / Whimper / ...) to a coarse emotional state
using a hand-written rule of thumb. Treat the output as a hint, not truth.

Once the labelling loop has produced real data, this can be replaced by (or
calibrated against) a learned model -- the interface stays the same.
"""

from __future__ import annotations

from dataclasses import dataclass

# AudioSet dog *vocalization* classes -> coarse emotional state (Japanese
# display). "Dog" / "Domestic animals, pets" are intentionally excluded: they
# say "a dog" but not *how* it sounded, so they carry no emotional signal.
EMOTION_BY_CLASS: dict[str, str] = {
    "Growling": "威嚇・警戒",
    "Bark": "警戒・興奮",
    "Bow-wow": "警戒・興奮",
    "Bay": "警戒",
    "Howl": "遠吠え（呼びかけ・寂しさ）",
    "Whimper (dog)": "不安・甘え",
    "Yip": "興奮・驚き",
}


@dataclass
class Emotion:
    """A coarse, heuristic emotional state derived from the vocalization type."""

    state: str | None  # JP display label, or None if undetermined
    basis: str | None  # the vocalization class that drove the guess
    score: float  # that class's score (0..1)

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "basis": self.basis,
            "score": round(self.score, 4),
        }


def infer_emotion(class_scores: dict[str, float], *, min_score: float = 0.0) -> Emotion:
    """Pick the highest-scoring expressive vocalization and map it to a state.

    `class_scores` should map AudioSet class names to scores; only the
    emotion-bearing vocalizations are considered.
    """
    best_name: str | None = None
    best_score = 0.0
    for name, score in class_scores.items():
        if name in EMOTION_BY_CLASS and score >= min_score and score > best_score:
            best_name, best_score = name, score
    if best_name is None:
        return Emotion(None, None, 0.0)
    return Emotion(EMOTION_BY_CLASS[best_name], best_name, best_score)
