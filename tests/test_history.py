"""Tests for the detection-history recorders (CSV is real; gsheet is faked)."""

from __future__ import annotations

import csv
import datetime as _dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from wan_pulse.config import HistoryConfig
from wan_pulse.history import COLUMNS, CsvRecorder, SheetWebhookRecorder, load_recorder


def _cls(is_dog=True, emotion="警戒・興奮"):
    return SimpleNamespace(
        is_dog=is_dog, dog_label="Dog", dog_score=0.68,
        emotion=emotion, emotion_basis="Bark", emotion_score=0.58,
        top_label="Bark", top_score=0.58,
    )


def _seg():
    return SimpleNamespace(duration_sec=1.9, peak_dbfs=-30.0)


WHEN = _dt.datetime(2026, 5, 30, 22, 1, 28)


def test_csv_writes_header_then_rows(tmp_path):
    path = tmp_path / "h" / "detections.csv"
    rec = CsvRecorder(path)
    assert rec.record(_cls(), Path("a.wav"), _seg(), when=WHEN) is True
    assert rec.record(_cls(emotion="不安・甘え"), Path("b.wav"), _seg(), when=WHEN) is True

    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == COLUMNS          # header written once
    assert len(rows) == 2
    assert rows[0]["file"] == "a.wav" and rows[0]["emotion"] == "警戒・興奮"
    assert rows[0]["is_dog"] == "True" and rows[0]["dog_score"] == "0.68"


def test_csv_only_dog_filter(tmp_path):
    path = tmp_path / "detections.csv"
    rec = CsvRecorder(path, only_dog=True)
    assert rec.record(_cls(is_dog=False), Path("x.wav"), _seg(), when=WHEN) is False
    assert not path.exists()  # nothing written, no header either


def test_sheet_recorder_posts_row():
    calls = []
    rec = SheetWebhookRecorder("http://script", transport=lambda u, p, timeout=10.0: calls.append((u, p)))
    assert rec.record(_cls(), Path("a.wav"), _seg(), when=WHEN) is True
    url, row = calls[0]
    assert url == "http://script"
    assert row["file"] == "a.wav" and row["emotion"] == "警戒・興奮"
    assert row["timestamp"] == "2026-05-30T22:01:28"


def test_sheet_failure_swallowed():
    def boom(url, payload, timeout=10.0):
        raise OSError("offline")

    rec = SheetWebhookRecorder("http://x", transport=boom)
    assert rec.record(_cls(), Path("a.wav"), _seg(), when=WHEN) is False


def test_load_recorder_selects_backend(tmp_path, monkeypatch):
    csv_rec = load_recorder(HistoryConfig(enabled=True, backend="csv",
                                          csv_path=str(tmp_path / "d.csv")))
    assert isinstance(csv_rec, CsvRecorder)

    monkeypatch.delenv("WP_SHEET", raising=False)
    with pytest.raises(ValueError):  # gsheet needs the env var
        load_recorder(HistoryConfig(enabled=True, backend="gsheet", webhook_env="WP_SHEET"))
    monkeypatch.setenv("WP_SHEET", "http://script/exec")
    sheet_rec = load_recorder(HistoryConfig(enabled=True, backend="gsheet", webhook_env="WP_SHEET"))
    assert isinstance(sheet_rec, SheetWebhookRecorder)
