"""Tests for the local web app: scanning, review saving, path safety, and a
live end-to-end round-trip over HTTP (stdlib only, no browser)."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from wan_pulse import web


def _make_segment(day_dir: Path, name: str, meta: dict | None = None) -> Path:
    day_dir.mkdir(parents=True, exist_ok=True)
    wav = day_dir / f"{name}.wav"
    wav.write_bytes(b"RIFFfake-wav-bytes")
    if meta is not None:
        wav.with_suffix(".json").write_text(json.dumps(meta), encoding="utf-8")
    return wav


def test_scan_pairs_wav_and_sidecar(tmp_path):
    _make_segment(tmp_path / "2026-05-30", "bark_a", {"is_dog": True, "emotion": "警戒・興奮"})
    _make_segment(tmp_path / "2026-05-30", "bark_b", None)  # no sidecar

    segs = web.scan_segments(tmp_path)
    assert len(segs) == 2
    by_name = {s["name"]: s for s in segs}
    assert by_name["bark_a.wav"]["meta"]["is_dog"] is True
    assert by_name["bark_a.wav"]["has_sidecar"] is True
    assert by_name["bark_b.wav"]["has_sidecar"] is False

    st = web.stats(segs)
    assert st == {"total": 2, "dogs": 1, "reviewed": 0}


def test_save_review_merges_into_sidecar(tmp_path):
    wav = _make_segment(tmp_path / "d", "bark_x", {"is_dog": False, "top_label": "Speech"})
    rel = wav.relative_to(tmp_path).as_posix()

    review = web.save_review(tmp_path, rel, {"is_dog": True, "emotion": "不安・甘え", "note": "夜中"})
    assert review["is_dog"] is True and review["emotion"] == "不安・甘え"
    assert "reviewed_at" in review

    data = json.loads(wav.with_suffix(".json").read_text(encoding="utf-8"))
    assert data["top_label"] == "Speech"        # original auto fields preserved
    assert data["review"]["note"] == "夜中"

    # reflected in scan + stats
    segs = web.scan_segments(tmp_path)
    assert web.stats(segs)["reviewed"] == 1


def test_save_review_creates_sidecar_when_missing(tmp_path):
    wav = _make_segment(tmp_path / "d", "bark_y", None)
    rel = wav.relative_to(tmp_path).as_posix()
    web.save_review(tmp_path, rel, {"is_dog": True})
    assert wav.with_suffix(".json").exists()


def test_path_traversal_blocked(tmp_path):
    with pytest.raises(PermissionError):
        web._safe_wav(tmp_path, "../../etc/passwd.wav")
    with pytest.raises(ValueError):
        web._safe_wav(tmp_path, "d/not_audio.txt")


def test_http_roundtrip(tmp_path):
    _make_segment(tmp_path / "2026-05-30", "bark_a", {"is_dog": True, "top_label": "Bark"})
    httpd = web.build_server(tmp_path, "127.0.0.1", 0)  # port 0 = pick a free port
    port = httpd.server_address[1]
    import threading
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        # index
        assert b"wan-pulse" in urllib.request.urlopen(base + "/").read()
        # segments API
        data = json.loads(urllib.request.urlopen(base + "/api/segments").read())
        assert data["stats"]["total"] == 1
        seg_id = data["segments"][0]["id"]
        # audio
        audio = urllib.request.urlopen(base + "/audio?path=" + seg_id).read()
        assert audio.startswith(b"RIFF")
        # review POST
        body = json.dumps({"path": seg_id, "is_dog": True, "emotion": "警戒・興奮"}).encode()
        req = urllib.request.Request(base + "/api/review", data=body,
                                     headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req).read())
        assert resp["ok"] is True and resp["review"]["emotion"] == "警戒・興奮"
    finally:
        httpd.shutdown()
