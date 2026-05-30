"""Notification stage: ping Slack when a dog is detected.

Uses a Slack **Incoming Webhook** (the simplest option for a personal channel:
no OAuth, no bot token -- just one secret URL). The POST is done with the
standard library, so this adds no dependencies.

The webhook URL is a secret and is read from an environment variable (never the
config file). Notifications run on the classification worker thread, so they are
already off the realtime audio path; failures are logged and never stop capture.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from abc import ABC, abstractmethod

from .config import NotifyConfig
from .logsetup import get_logger

log = get_logger()


class Notifier(ABC):
    @abstractmethod
    def notify(self, classification, wav_path, segment) -> bool:
        """Send a notification for a segment. Returns True if actually sent."""
        ...


def build_message(classification, wav_path, segment) -> str:
    """Human-readable Slack message for a detected segment."""
    c = classification
    if c.emotion:
        basis = f"（{c.emotion_basis} {c.emotion_score:.2f}）" if c.emotion_basis else ""
        emotion_line = f"感情: {c.emotion}{basis}"
    else:
        emotion_line = "感情: （不明）"
    dog = f"{c.dog_label} {c.dog_score:.2f}" if c.dog_label else "—"
    return (
        "🐕 ワンパルス: 犬を検知\n"
        f"{emotion_line}\n"
        f"犬らしさ: {dog}\n"
        f"ファイル: {wav_path.name}（{segment.duration_sec:.1f}s, "
        f"peak {segment.peak_dbfs:.1f} dBFS）"
    )


def _post_json(url: str, payload: dict, timeout: float = 5.0) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout):  # raises on HTTP error
        pass


class SlackNotifier(Notifier):
    """Posts to a Slack Incoming Webhook, with filtering and a cooldown."""

    def __init__(
        self,
        webhook_url: str,
        *,
        only_dog: bool = True,
        min_dog_score: float = 0.0,
        cooldown_sec: float = 30.0,
        clock=time.monotonic,
        transport=_post_json,
    ) -> None:
        self.webhook_url = webhook_url
        self.only_dog = only_dog
        self.min_dog_score = min_dog_score
        self.cooldown_sec = cooldown_sec
        self._clock = clock
        self._transport = transport
        self._last_sent: float | None = None

    def _passes_filter(self, classification) -> bool:
        c = classification
        if self.only_dog and not c.is_dog:
            return False
        if c.dog_score < self.min_dog_score:
            return False
        if self._last_sent is not None:
            if self._clock() - self._last_sent < self.cooldown_sec:
                return False
        return True

    def notify(self, classification, wav_path, segment) -> bool:
        if not self._passes_filter(classification):
            return False
        try:
            self._transport(self.webhook_url, {"text": build_message(classification, wav_path, segment)})
        except Exception as exc:  # noqa: BLE001 - never let notification break capture
            log.error("[wan-pulse] Slack notify failed: %s", exc)
            return False
        self._last_sent = self._clock()
        log.info("[wan-pulse] notified Slack: %s", wav_path.name)
        return True


def load_notifier(config: NotifyConfig) -> Notifier:
    """Build a SlackNotifier, reading the secret webhook URL from the env var."""
    url = os.environ.get(config.webhook_env, "").strip()
    if not url:
        raise ValueError(
            f"Slack webhook URL not set. Export it, e.g. "
            f"`export {config.webhook_env}=https://hooks.slack.com/services/...`"
        )
    return SlackNotifier(
        url,
        only_dog=config.only_dog,
        min_dog_score=config.min_dog_score,
        cooldown_sec=config.cooldown_sec,
    )
