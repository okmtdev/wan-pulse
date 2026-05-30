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
    config = CaptureConfig(output_dir=str(tmp_path), threshold_db=-37.5, device="M-305")
    path = configfile.write_run_log(config)
    assert path.exists() and path.suffix == ".toml"
    # The snapshot must parse back into an equivalent config.
    values = configfile.load_file_values(path)
    assert values["threshold_db"] == -37.5
    assert values["device"] == "M-305"


def test_run_log_handles_none_device(tmp_path):
    config = CaptureConfig(output_dir=str(tmp_path), device=None)
    path = configfile.write_run_log(config)
    values = configfile.load_file_values(path)
    # device is None -> rendered as a comment -> absent on reload.
    assert "device" not in values
