"""Tests for TOML config loading, merge precedence, and the run log."""

from __future__ import annotations

from pathlib import Path

import pytest

from wan_pulse import configfile
from wan_pulse.config import CaptureConfig


def test_template_is_valid_loadable_toml(tmp_path):
    path = tmp_path / configfile.DEFAULT_CONFIG_NAME
    path.write_text(configfile.template_toml(), encoding="utf-8")
    values = configfile.load_file_values(path)
    # Template starts with a deliberately low threshold for tuning.
    assert values["threshold_db"] == -60.0
    # Re-building a config from the template must succeed.
    CaptureConfig(**values)


def test_load_rejects_unknown_keys(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('threshold_db = -30.0\nbogus = 1\n', encoding="utf-8")
    with pytest.raises(ValueError):
        configfile.load_file_values(path)


def test_precedence_defaults_file_cli():
    file_values = {"threshold_db": -50.0, "pre_margin_sec": 0.2}
    # CLI overrides file; None means "not given" and is ignored.
    overrides = {"threshold_db": -30.0, "pre_margin_sec": None, "samplerate": None}
    config = configfile.resolve_config(file_values, overrides)
    assert config.threshold_db == -30.0          # from CLI
    assert config.pre_margin_sec == 0.2          # from file
    assert config.samplerate == CaptureConfig.samplerate  # built-in default


def test_find_config_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert configfile.find_config(None) is None
    Path(configfile.DEFAULT_CONFIG_NAME).write_text("threshold_db = -42.0\n")
    assert configfile.find_config(None) == Path(configfile.DEFAULT_CONFIG_NAME)


def test_load_missing_explicit_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        configfile.load_file_values(tmp_path / "nope.toml")


def test_run_log_roundtrips(tmp_path):
    config = CaptureConfig(run_log_dir=str(tmp_path), threshold_db=-37.5, device="M-305")
    path = configfile.write_run_log(config)
    assert path.exists() and path.suffix == ".toml"
    assert path.parent == tmp_path  # written to run_log_dir, not output_dir
    # The snapshot must parse back into an equivalent config.
    values = configfile.load_file_values(path)
    assert values["threshold_db"] == -37.5
    assert values["device"] == "M-305"


def test_run_log_handles_none_device(tmp_path):
    config = CaptureConfig(run_log_dir=str(tmp_path), device=None)
    path = configfile.write_run_log(config)
    values = configfile.load_file_values(path)
    # device is None -> rendered as a comment -> absent on reload.
    assert "device" not in values


def test_classify_table_parsed_separately(tmp_path):
    path = tmp_path / "wan-pulse.toml"
    path.write_text(
        'threshold_db = -30.0\n\n[classify]\nenabled = true\ndog_threshold = 0.5\n',
        encoding="utf-8",
    )
    # The [classify] table must not trip the audio-key validator...
    audio = configfile.load_file_values(path)
    assert audio["threshold_db"] == -30.0 and "classify" not in audio
    # ...and is read by the dedicated loader.
    classify = configfile.load_classify_values(path)
    assert classify == {"enabled": True, "dog_threshold": 0.5}


def test_resolve_classify_precedence():
    from wan_pulse.config import ClassifyConfig

    cfg = configfile.resolve_classify(
        {"enabled": True, "dog_threshold": 0.5},
        {"dog_threshold": 0.2, "model_path": None},
    )
    assert cfg.enabled is True            # from file
    assert cfg.dog_threshold == 0.2       # CLI override
    assert cfg.model_path == ClassifyConfig.model_path  # default


def test_template_includes_classify_table(tmp_path):
    path = tmp_path / configfile.DEFAULT_CONFIG_NAME
    path.write_text(configfile.template_toml(), encoding="utf-8")
    text = path.read_text()
    assert "[classify]" in text and "[notify]" in text
    # Template must round-trip through all loaders.
    configfile.load_file_values(path)
    assert configfile.load_classify_values(path)["enabled"] is False
    assert configfile.load_notify_values(path)["enabled"] is False


def test_notify_table_parsed_separately(tmp_path):
    path = tmp_path / "wan-pulse.toml"
    path.write_text(
        'threshold_db = -30.0\n\n[notify]\nenabled = true\ncooldown_sec = 60.0\n',
        encoding="utf-8",
    )
    # [notify] must not trip the audio-key validator.
    assert "notify" not in configfile.load_file_values(path)
    assert configfile.load_notify_values(path) == {"enabled": True, "cooldown_sec": 60.0}
    cfg = configfile.resolve_notify(
        configfile.load_notify_values(path), {"enabled": None}
    )
    assert cfg.enabled is True and cfg.cooldown_sec == 60.0
