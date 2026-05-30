"""Detection-history sink: append one row per classified segment.

Two backends, sharing a common row schema:

- **gsheet**: POST the row as JSON to a Google Apps Script *web app* URL, which
  appends it to a Google Sheet. Mirrors the Slack-webhook style (a secret URL in
  an env var, stdlib POST, no dependencies).
- **csv**: append to a local CSV file. Zero setup, works offline, opens in any
  spreadsheet app.

Runs on the classification worker thread, so it's off the realtime audio path.
Failures are logged and never stop capture.
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import os
import threading
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path

from .config import HistoryConfig
from .logsetup import get_logger

log = get_logger()

# Column order for both the CSV header and the JSON row keys.
COLUMNS = [
    "timestamp", "file", "duration_sec", "peak_dbfs",
    "is_dog", "dog_label", "dog_score",
    "emotion", "emotion_basis", "emotion_score",
    "top_label", "top_score",
    "is_animal", "animal_label", "animal_score",
]


def build_row(classification, wav_path, segment, when: _dt.datetime) -> dict:
    c = classification
    return {
        "timestamp": when.isoformat(timespec="seconds"),
        "file": wav_path.name,
        "duration_sec": round(segment.duration_sec, 3),
        "peak_dbfs": round(segment.peak_dbfs, 2),
        "is_dog": c.is_dog,
        "dog_label": c.dog_label,
        "dog_score": round(c.dog_score, 4),
        "emotion": c.emotion,
        "emotion_basis": c.emotion_basis,
        "emotion_score": round(c.emotion_score, 4),
        "top_label": c.top_label,
        "top_score": round(c.top_score, 4),
        "is_animal": getattr(c, "is_animal", False),
        "animal_label": getattr(c, "animal_label", None),
        "animal_score": round(getattr(c, "animal_score", 0.0), 4),
    }


class HistoryRecorder(ABC):
    """Shared filtering; subclasses implement `_write(row)`."""

    def __init__(self, *, only_dog: bool = False) -> None:
        self.only_dog = only_dog

    def record(self, classification, wav_path, segment, when: _dt.datetime | None = None) -> bool:
        is_detection = classification.is_dog or getattr(classification, "is_animal", False)
        if self.only_dog and not is_detection:
            return False
        row = build_row(classification, wav_path, segment, when or _dt.datetime.now())
        try:
            self._write(row)
        except Exception as exc:  # noqa: BLE001 - never let history break capture
            log.error("[wan-pulse] history record failed: %s", exc)
            return False
        return True

    def _write(self, row: dict) -> None:
        raise NotImplementedError


class CsvRecorder(HistoryRecorder):
    """Appends rows to a local CSV (writing a header for a new file)."""

    def __init__(self, path: str | Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self.path = Path(path)
        self._lock = threading.Lock()

    def _write(self, row: dict) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            is_new = not self.path.exists() or self.path.stat().st_size == 0
            with self.path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=COLUMNS)
                if is_new:
                    writer.writeheader()
                writer.writerow(row)


def _post_json(url: str, payload: dict, timeout: float = 10.0) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout):  # raises on HTTP error
        pass


class SheetWebhookRecorder(HistoryRecorder):
    """POSTs each row to a Google Apps Script web app that appends to a Sheet."""

    def __init__(self, url: str, *, transport=_post_json, **kwargs) -> None:
        super().__init__(**kwargs)
        self.url = url
        self._transport = transport

    def _write(self, row: dict) -> None:
        self._transport(self.url, row)


def load_recorder(config: HistoryConfig) -> HistoryRecorder:
    """Build the configured recorder.

    For the gsheet backend the URL may be set directly in config (history.webhook_url)
    or via its env var; the direct value wins.
    """
    common = dict(only_dog=config.only_dog)
    if config.backend == "csv":
        return CsvRecorder(config.csv_path, **common)
    url = config.webhook_url.strip() or os.environ.get(config.webhook_env, "").strip()
    if not url:
        raise ValueError(
            f"Sheet web-app URL not set. Set history.webhook_url or "
            f"`export {config.webhook_env}=https://script.google.com/macros/s/.../exec`"
        )
    return SheetWebhookRecorder(url, **common)
