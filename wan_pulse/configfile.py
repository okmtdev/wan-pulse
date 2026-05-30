"""TOML config file support and the run log.

Why this exists:
- Tuning values (threshold, margins, ...) should be easy to re-set without
  retyping CLI flags every time, so we load them from a ``wan-pulse.toml`` file.
- For reproducibility, every `run` also drops a one-shot "run log" recording the
  *effective* settings that were actually used, so you can correlate a batch of
  recordings with the config that produced them.

Precedence (lowest to highest): built-in defaults  <  TOML file  <  CLI flags.

Only reading TOML needs a parser (``tomllib`` on 3.11+, ``tomli`` otherwise).
Writing is done by hand so generating the template/run log needs no extra deps.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
from pathlib import Path
from typing import Any

from .config import CaptureConfig, ClassifyConfig, NotifyConfig

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.9/3.10
    import tomli as _toml  # type: ignore[no-redef]


DEFAULT_CONFIG_NAME = "wan-pulse.toml"

# Inference / notification settings live under their own tables so they're
# clearly separate from the audio-capture keys.
CLASSIFY_TABLE = "classify"
NOTIFY_TABLE = "notify"

_CLASSIFY_COMMENTS: dict[str, str] = {
    "enabled": "true で保存区間を推論(犬か/発声タイプ)。要 .[infer] とモデル",
    "model_path": "YAMNet TFLite モデル (scripts/download-yamnet.sh)",
    "labels_path": "AudioSet クラスマップ CSV",
    "dog_threshold": "犬クラスのスコアがこれ以上で「犬」と判定",
    "top_k": "サイドカー JSON に残す上位ラベル数",
}

_NOTIFY_COMMENTS: dict[str, str] = {
    "enabled": "true で犬検知時に Slack 通知(要 classify 有効)",
    "webhook_env": "Slack Webhook URL を入れる環境変数名(URL自体はここに書かない)",
    "only_dog": "犬と判定された区間だけ通知",
    "min_dog_score": "犬スコアがこれ以上のときだけ通知",
    "cooldown_sec": "連続通知の最小間隔(秒)。鳴き続けても spam しない",
}

# Per-field inline comments used when rendering the template / run log.
_FIELD_COMMENTS: dict[str, str] = {
    "samplerate": "Hz. M-305 が拒否する場合は 44100 / 48000 を試す",
    "channels": "1 = mono",
    "block_ms": "1ブロックの長さ(ms)。検知の時間分解能",
    "device": "入力デバイス(番号 or 名前の一部)。未指定=既定入力",
    "threshold_db": "検知の閾値(dBFS)。低いほど敏感。まず低めで全部録り、保存ファイルの peak を見て上げる",
    "pre_margin_sec": "検知の前に残す秒数(鳴き始めの切れ防止)",
    "post_margin_sec": "この秒数ぶん静かになったら区間を閉じる",
    "min_segment_sec": "これより短い区間はノイズとして破棄",
    "max_segment_sec": "1区間の最大長(暴走防止)",
    "output_dir": ".wav の保存先",
    "run_log_dir": "run_*.toml(設定スナップショット)の保存先",
}

# A low starting threshold so nothing is missed while you tune. Raise it once
# you've looked at the peak dBFS values embedded in the saved filenames.
_TEMPLATE_OVERRIDES: dict[str, Any] = {"threshold_db": -60.0}


def _toml_value(value: Any) -> str:
    """Render a Python value as a TOML scalar."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    # Strings (and anything else) -> JSON gives us valid TOML basic-string quoting.
    return json.dumps(str(value))


def _render_fields(config: Any, comments: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for field in dataclasses.fields(config):
        comment = comments.get(field.name, "")
        value = getattr(config, field.name)
        suffix = f"  # {comment}" if comment else ""
        if value is None:
            # TOML has no null; leave it commented so the default (None) applies.
            lines.append(f"# {field.name} =   # {comment}".rstrip())
        else:
            lines.append(f"{field.name} = {_toml_value(value)}{suffix}")
    return lines


def render_toml(
    config: CaptureConfig,
    *,
    header_lines: list[str] | None = None,
    classify: ClassifyConfig | None = None,
    notify: NotifyConfig | None = None,
) -> str:
    """Render a CaptureConfig (+ optional [classify]/[notify] tables) as TOML."""
    lines: list[str] = []
    for line in header_lines or []:
        lines.append(f"# {line}")
    if header_lines:
        lines.append("")

    lines.extend(_render_fields(config, _FIELD_COMMENTS))

    if classify is not None:
        lines.append("")
        lines.append(f"[{CLASSIFY_TABLE}]")
        lines.extend(_render_fields(classify, _CLASSIFY_COMMENTS))
    if notify is not None:
        lines.append("")
        lines.append(f"[{NOTIFY_TABLE}]")
        lines.extend(_render_fields(notify, _NOTIFY_COMMENTS))
    return "\n".join(lines) + "\n"


def template_toml() -> str:
    """The default config to write with `wan-pulse init`."""
    config = dataclasses.replace(CaptureConfig(), **_TEMPLATE_OVERRIDES)
    header = [
        "wan-pulse 設定ファイル",
        "値を編集して保存すれば次回 run から反映されます(CLI フラグが最優先)。",
        "チューニングのコツ: まず threshold_db を低め(全部録る)にして起動し、",
        "保存された .wav のファイル名にある peak(dBFS) を見て徐々に上げていく。",
    ]
    return render_toml(
        config, header_lines=header, classify=ClassifyConfig(), notify=NotifyConfig()
    )


def find_config(explicit: str | None) -> Path | None:
    """Resolve which config file to use, if any."""
    if explicit:
        return Path(explicit)
    candidate = Path(DEFAULT_CONFIG_NAME)
    return candidate if candidate.is_file() else None


def _read_toml(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    if not path.is_file():
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open("rb") as fh:
        return _toml.load(fh)


def load_file_values(path: Path | None) -> dict[str, Any]:
    """Read known CaptureConfig fields from a TOML file (empty dict if None).

    The optional [classify]/[notify] tables are ignored here (read separately).
    """
    data = _read_toml(path)
    valid = {f.name for f in dataclasses.fields(CaptureConfig)}
    unknown = set(data) - valid - {CLASSIFY_TABLE, NOTIFY_TABLE}
    if unknown:
        raise ValueError(f"unknown config keys in {path}: {', '.join(sorted(unknown))}")
    return {k: v for k, v in data.items() if k in valid}


def _load_table(path: Path | None, name: str, config_cls) -> dict[str, Any]:
    table = _read_toml(path).get(name, {})
    if not isinstance(table, dict):
        raise ValueError(f"[{name}] must be a table in {path}")
    valid = {f.name for f in dataclasses.fields(config_cls)}
    unknown = set(table) - valid
    if unknown:
        raise ValueError(f"unknown [{name}] keys in {path}: {', '.join(sorted(unknown))}")
    return {k: v for k, v in table.items() if k in valid}


def load_classify_values(path: Path | None) -> dict[str, Any]:
    """Read the [classify] table from a TOML file (empty dict if absent)."""
    return _load_table(path, CLASSIFY_TABLE, ClassifyConfig)


def load_notify_values(path: Path | None) -> dict[str, Any]:
    """Read the [notify] table from a TOML file (empty dict if absent)."""
    return _load_table(path, NOTIFY_TABLE, NotifyConfig)


def resolve_config(
    file_values: dict[str, Any], cli_overrides: dict[str, Any]
) -> CaptureConfig:
    """Merge built-in defaults < file < CLI (overrides with value None are ignored)."""
    base = CaptureConfig(**file_values)
    applied = {k: v for k, v in cli_overrides.items() if v is not None}
    return dataclasses.replace(base, **applied) if applied else base


def resolve_classify(
    file_values: dict[str, Any], cli_overrides: dict[str, Any]
) -> ClassifyConfig:
    """Same precedence as resolve_config, for the inference settings."""
    base = ClassifyConfig(**file_values)
    applied = {k: v for k, v in cli_overrides.items() if v is not None}
    return dataclasses.replace(base, **applied) if applied else base


def resolve_notify(
    file_values: dict[str, Any], cli_overrides: dict[str, Any]
) -> NotifyConfig:
    """Same precedence as resolve_config, for the notification settings."""
    base = NotifyConfig(**file_values)
    applied = {k: v for k, v in cli_overrides.items() if v is not None}
    return dataclasses.replace(base, **applied) if applied else base


def write_run_log(
    config: CaptureConfig,
    *,
    classify: ClassifyConfig | None = None,
    notify: NotifyConfig | None = None,
    when: _dt.datetime | None = None,
) -> Path:
    """Drop a TOML snapshot of the effective config under the output dir."""
    when = when or _dt.datetime.now()
    out_dir = Path(config.run_log_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / when.strftime("run_%Y%m%d_%H%M%S.toml")
    header = [
        f"wan-pulse run @ {when.isoformat(timespec='seconds')}",
        "この run で実際に使われた設定のスナップショット(再現用)。",
        "良い値が見つかったら wan-pulse.toml にコピーして使い回せます。",
    ]
    path.write_text(
        render_toml(config, header_lines=header, classify=classify, notify=notify),
        encoding="utf-8",
    )
    return path
