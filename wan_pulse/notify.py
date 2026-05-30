"""Notification stage: ping Slack when a dog is detected.

Two transports, sharing the same filtering/cooldown logic:

- **Webhook (text only)**: a Slack Incoming Webhook URL. Simplest, but cannot
  attach files.
- **Bot token (file upload)**: uploads the .wav itself using Slack's external
  upload flow (files.getUploadURLExternal -> upload -> completeUploadExternal),
  so you can actually hear the bark in Slack. Needs a bot token and channel ID.

Everything uses the standard library (urllib), so no extra dependencies. The
secret (webhook URL or bot token) is read from an environment variable, never
the config file. Notifications run on the classification worker thread; failures
are logged and never stop capture.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import urlencode

from .config import NotifyConfig
from .logsetup import get_logger

log = get_logger()

_SLACK_API = "https://slack.com/api/"


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


# --- HTTP helpers (stdlib only) ------------------------------------------------

def _post_json(url: str, payload: dict, timeout: float = 5.0) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout):  # raises on HTTP error
        pass


def _slack_api(method: str, token: str, params: dict, *, post: bool, timeout: float = 10.0) -> dict:
    """Call a Slack Web API method with a bearer token; raise unless ok."""
    headers = {"Authorization": f"Bearer {token}"}
    if post:
        body = urlencode(params).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(_SLACK_API + method, data=body, headers=headers)
    else:
        req = urllib.request.Request(f"{_SLACK_API}{method}?{urlencode(params)}", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(f"Slack {method} failed: {payload.get('error')}")
    return payload


def _post_file(upload_url: str, filename: str, data: bytes, timeout: float = 15.0) -> None:
    """Upload raw file bytes to a Slack upload_url as multipart/form-data."""
    boundary = "----wanpulse" + os.urandom(8).hex()
    crlf = "\r\n"
    body = bytearray()
    body += f"--{boundary}{crlf}".encode()
    body += (
        f'Content-Disposition: form-data; name="file"; filename="{filename}"{crlf}'
        f"Content-Type: application/octet-stream{crlf}{crlf}"
    ).encode()
    body += data + crlf.encode()
    body += f"--{boundary}--{crlf}".encode()
    req = urllib.request.Request(
        upload_url, data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=timeout):
        pass


def _slack_upload_file(token: str, channel: str, file_path: Path, comment: str) -> None:
    """Slack's 3-step external upload: get URL -> upload bytes -> complete."""
    data = Path(file_path).read_bytes()
    res = _slack_api(
        "files.getUploadURLExternal", token,
        {"filename": file_path.name, "length": str(len(data))}, post=False,
    )
    _post_file(res["upload_url"], file_path.name, data)
    _slack_api(
        "files.completeUploadExternal", token,
        {
            "files": json.dumps([{"id": res["file_id"], "title": file_path.name}]),
            "channel_id": channel,
            "initial_comment": comment,
        },
        post=True,
    )


# --- Notifiers -----------------------------------------------------------------

class _BaseSlackNotifier(Notifier):
    """Shared filtering + cooldown; subclasses implement `_send`."""

    def __init__(
        self,
        *,
        only_dog: bool = True,
        min_dog_score: float = 0.0,
        cooldown_sec: float = 30.0,
        clock=time.monotonic,
    ) -> None:
        self.only_dog = only_dog
        self.min_dog_score = min_dog_score
        self.cooldown_sec = cooldown_sec
        self._clock = clock
        self._last_sent: float | None = None

    def _passes_filter(self, classification) -> bool:
        c = classification
        if self.only_dog and not c.is_dog:
            return False
        if c.dog_score < self.min_dog_score:
            return False
        if self._last_sent is not None and self._clock() - self._last_sent < self.cooldown_sec:
            return False
        return True

    def notify(self, classification, wav_path, segment) -> bool:
        if not self._passes_filter(classification):
            return False
        try:
            self._send(classification, wav_path, segment)
        except Exception as exc:  # noqa: BLE001 - never let notification break capture
            log.error("[wan-pulse] Slack notify failed: %s", exc)
            return False
        self._last_sent = self._clock()
        log.info("[wan-pulse] notified Slack: %s", wav_path.name)
        return True

    def _send(self, classification, wav_path, segment) -> None:
        raise NotImplementedError


class SlackNotifier(_BaseSlackNotifier):
    """Text-only notification via a Slack Incoming Webhook."""

    def __init__(self, webhook_url: str, *, transport=_post_json, **kwargs) -> None:
        super().__init__(**kwargs)
        self.webhook_url = webhook_url
        self._transport = transport

    def _send(self, classification, wav_path, segment) -> None:
        self._transport(self.webhook_url, {"text": build_message(classification, wav_path, segment)})


class SlackFileNotifier(_BaseSlackNotifier):
    """Uploads the .wav (with the message as caption) via a Slack bot token."""

    def __init__(self, token: str, channel: str, *, uploader=_slack_upload_file, **kwargs) -> None:
        super().__init__(**kwargs)
        self.token = token
        self.channel = channel
        self._uploader = uploader

    def _send(self, classification, wav_path, segment) -> None:
        caption = build_message(classification, wav_path, segment)
        self._uploader(self.token, self.channel, Path(wav_path), caption)


def _secret(direct: str, env_name: str) -> str:
    """Prefer a value written directly in config; else read the env var."""
    return direct.strip() or os.environ.get(env_name, "").strip()


def load_notifier(config: NotifyConfig) -> Notifier:
    """Build the right notifier from config + environment.

    The secret (webhook URL / bot token) may be set directly in the config or
    via its env var; the direct value wins.
    """
    common = dict(
        only_dog=config.only_dog,
        min_dog_score=config.min_dog_score,
        cooldown_sec=config.cooldown_sec,
    )
    if config.attach_audio:
        token = _secret(config.bot_token, config.bot_token_env)
        if not token:
            raise ValueError(
                f"attach_audio=true needs a bot token. Set notify.bot_token or "
                f"`export {config.bot_token_env}=xoxb-...`"
            )
        if not config.channel:
            raise ValueError("attach_audio=true needs a channel ID (notify.channel = \"C0123...\")")
        return SlackFileNotifier(token, config.channel, **common)

    url = _secret(config.webhook_url, config.webhook_env)
    if not url:
        raise ValueError(
            f"Slack webhook URL not set. Set notify.webhook_url or "
            f"`export {config.webhook_env}=https://hooks.slack.com/services/...`"
        )
    return SlackNotifier(url, **common)
