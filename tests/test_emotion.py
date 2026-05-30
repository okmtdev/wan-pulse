"""Tests for the rule-based emotion guess (pure, no model needed)."""

from __future__ import annotations

import numpy as np

from wan_pulse.classify import scores_to_classification
from wan_pulse.emotion import infer_emotion


def test_growling_maps_to_threat():
    e = infer_emotion({"Growling": 0.6, "Bark": 0.2})
    assert e.state == "威嚇・警戒"
    assert e.basis == "Growling"
    assert e.score == 0.6


def test_picks_highest_expressive_class():
    e = infer_emotion({"Bark": 0.4, "Whimper (dog)": 0.7})
    assert e.basis == "Whimper (dog)" and e.state == "不安・甘え"


def test_no_expressive_class_is_undetermined():
    # "Dog" / "Domestic animals, pets" carry no emotional signal.
    e = infer_emotion({"Dog": 0.9, "Domestic animals, pets": 0.8})
    assert e.state is None and e.basis is None and e.score == 0.0


# Indices line up with these labels.
LABELS = ["Speech", "Dog", "Bark", "Cat", "Growling", "Whimper (dog)"]


def test_classification_includes_emotion_for_dog():
    # Bark + Growling high -> dog, emotion from the strongest expressive class.
    scores = np.array([0.1, 0.5, 0.6, 0.0, 0.8, 0.05])
    c = scores_to_classification(scores, LABELS, top_k=5, dog_threshold=0.3)
    assert c.is_dog is True
    assert c.emotion == "威嚇・警戒"  # Growling (0.8) wins
    assert c.emotion_basis == "Growling"
    assert "感情" in c.summary()
    assert c.to_dict()["emotion"] == "威嚇・警戒"


def test_no_emotion_when_not_dog():
    scores = np.array([0.9, 0.05, 0.02, 0.0, 0.01, 0.0])  # Speech, no dog
    c = scores_to_classification(scores, LABELS, top_k=5, dog_threshold=0.3)
    assert c.is_dog is False
    assert c.emotion is None
    assert c.to_dict()["emotion"] is None
    assert "感情" not in c.summary()
