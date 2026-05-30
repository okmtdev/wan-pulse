"""Tests for the Slack notifier (no network: a fake transport captures posts)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from wan_pulse.notify import SlackFileNotifier, SlackNotifier, build_message, load_notifier
from wan_pulse.config import NotifyConfig


def _dog(emotion="警戒・興奮", is_dog=True, dog_score=0.7):
    return SimpleNamespace(
        is_dog=is_dog, dog_label="Dog", dog_score=dog_score,
        emotion=emotion, emotion_basis="Bark", emotion_score=0.58,
        top_label="Bark", top_score=0.58,
    )


def _seg():
    return SimpleNamespace(duration_sec=1.5, peak_dbfs=-12.3)


class FakeTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, url, payload, timeout=5.0):
        self.calls.append((url, payload))


def _notifier(transport, **kw):
    clock = kw.pop("clock", lambda: 0.0)
    return SlackNotifier("http://hook", transport=transport, clock=clock, **kw)


def test_build_message_contains_key_facts():
    msg = build_message(_dog(), Path("bark_x.wav"), _seg())
    assert "犬" in msg and "警戒・興奮" in msg and "bark_x.wav" in msg


def test_notifies_for_dog():
    t = FakeTransport()
    sent = _notifier(t).notify(_dog(), Path("a.wav"), _seg())
    assert sent is True and len(t.calls) == 1
    assert t.calls[0][0] == "http://hook"
    assert "text" in t.calls[0][1]


def test_skips_non_dog_when_only_dog():
    t = FakeTransport()
    sent = _notifier(t, only_dog=True).notify(_dog(is_dog=False), Path("a.wav"), _seg())
    assert sent is False and t.calls == []


def test_min_dog_score_gate():
    t = FakeTransport()
    n = _notifier(t, min_dog_score=0.8)
    assert n.notify(_dog(dog_score=0.5), Path("a.wav"), _seg()) is False
    assert t.calls == []


def test_cooldown_blocks_second_call():
    t = FakeTransport()
    clock_val = {"t": 0.0}
    n = _notifier(t, cooldown_sec=30.0, clock=lambda: clock_val["t"])

    assert n.notify(_dog(), Path("a.wav"), _seg()) is True
    clock_val["t"] = 10.0  # within cooldown
    assert n.notify(_dog(), Path("b.wav"), _seg()) is False
    clock_val["t"] = 40.0  # past cooldown
    assert n.notify(_dog(), Path("c.wav"), _seg()) is True
    assert len(t.calls) == 2


def test_transport_failure_is_swallowed():
    def boom(url, payload, timeout=5.0):
        raise OSError("network down")

    sent = _notifier(boom).notify(_dog(), Path("a.wav"), _seg())
    assert sent is False  # logged, not raised


# --- file-upload notifier (fake uploader, no Slack) ---

def test_file_notifier_uploads_with_caption():
    calls = []

    def fake_uploader(token, channel, path, caption):
        calls.append((token, channel, path, caption))

    n = SlackFileNotifier("xoxb-1", "C123", uploader=fake_uploader, clock=lambda: 0.0)
    assert n.notify(_dog(), Path("a.wav"), _seg()) is True
    token, channel, path, caption = calls[0]
    assert token == "xoxb-1" and channel == "C123"
    assert path == Path("a.wav") and "犬" in caption


def test_file_notifier_respects_cooldown():
    calls = []
    clock_val = {"t": 0.0}
    n = SlackFileNotifier(
        "xoxb-1", "C123",
        uploader=lambda *a: calls.append(a),
        cooldown_sec=30.0, clock=lambda: clock_val["t"],
    )
    assert n.notify(_dog(), Path("a.wav"), _seg()) is True
    clock_val["t"] = 5.0
    assert n.notify(_dog(), Path("b.wav"), _seg()) is False
    assert len(calls) == 1


def test_load_notifier_picks_transport(monkeypatch):
    # attach_audio -> needs bot token + channel
    monkeypatch.delenv("WP_BOT", raising=False)
    cfg = NotifyConfig(enabled=True, attach_audio=True, bot_token_env="WP_BOT", channel="C1")
    with pytest.raises(ValueError):
        load_notifier(cfg)
    monkeypatch.setenv("WP_BOT", "xoxb-abc")
    assert isinstance(load_notifier(cfg), SlackFileNotifier)

    # attach_audio but missing channel
    cfg2 = NotifyConfig(enabled=True, attach_audio=True, bot_token_env="WP_BOT", channel="")
    with pytest.raises(ValueError):
        load_notifier(cfg2)

    # webhook path
    monkeypatch.setenv("WP_HOOK", "http://hook")
    cfg3 = NotifyConfig(enabled=True, webhook_env="WP_HOOK")
    assert isinstance(load_notifier(cfg3), SlackNotifier)
