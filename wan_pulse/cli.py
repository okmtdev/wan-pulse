"""Command-line entry point for wan-pulse.

Subcommands:
  init     write a starter wan-pulse.toml you can edit to re-tune easily
  devices  list available audio input/output devices
  monitor  print live RMS levels (use this to calibrate threshold_db)
  run      capture, skip silence, and save bark segments to .wav

Settings come from a TOML file (default: ./wan-pulse.toml) and any CLI flag
overrides it. Precedence: built-in defaults < TOML file < CLI flags.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .capture import Capture
from .config import CaptureConfig
from . import configfile
from .logsetup import DEFAULT_LOG_FILE, get_logger, setup_logging

log = get_logger()

# Commands whose "processing" should be written to the log file.
_FILE_LOG_COMMANDS = {"run", "monitor", "classify"}


def _add_log_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--log-file", default=DEFAULT_LOG_FILE,
                   help="processing log file ('none' to disable; default: %(default)s)")
    p.add_argument("--log-level", default="INFO",
                   help="log verbosity: DEBUG/INFO/WARNING/ERROR (default: %(default)s)")


# CLI flags default to None so we can tell "not given" from "given the default",
# and only override the config-file value when the user actually passed a flag.
def _add_audio_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", default=None,
                   help=f"path to TOML config (default: ./{configfile.DEFAULT_CONFIG_NAME} if present)")
    p.add_argument("--samplerate", type=int, default=None,
                   help="sample rate in Hz")
    p.add_argument("--channels", type=int, default=None,
                   help="input channels")
    p.add_argument("--block-ms", dest="block_ms", type=float, default=None,
                   help="block size in ms / detection resolution")
    p.add_argument("--device", default=None,
                   help="input device index or name substring")
    p.add_argument("--threshold-db", dest="threshold_db", type=float, default=None,
                   help="activation threshold in dBFS (lower = more sensitive)")


def _coerce_device(device):
    if isinstance(device, str) and device.isdigit():
        return int(device)
    return device


def _resolve(args: argparse.Namespace) -> CaptureConfig:
    """Build the effective config: defaults < TOML file < CLI flags."""
    config_path = configfile.find_config(getattr(args, "config", None))
    file_values = configfile.load_file_values(config_path)

    overrides = {
        "samplerate": args.samplerate,
        "channels": args.channels,
        "block_ms": args.block_ms,
        "device": _coerce_device(args.device),
        "threshold_db": args.threshold_db,
        "pre_margin_sec": getattr(args, "pre_margin", None),
        "post_margin_sec": getattr(args, "post_margin", None),
        "min_segment_sec": getattr(args, "min_segment", None),
        "max_segment_sec": getattr(args, "max_segment", None),
        "output_dir": getattr(args, "output_dir", None),
        "run_log_dir": getattr(args, "run_log_dir", None),
    }
    config = configfile.resolve_config(file_values, overrides)
    if config_path is not None:
        log.info("[wan-pulse] config: %s", config_path)
    return config


def _resolve_classify(args: argparse.Namespace, *, force_enabled: bool = False):
    """Build the ClassifyConfig: defaults < [classify] table < CLI flags."""
    config_path = configfile.find_config(getattr(args, "config", None))
    file_values = configfile.load_classify_values(config_path)
    overrides = {
        "enabled": True if force_enabled else getattr(args, "classify", None),
        "model_path": getattr(args, "model", None),
        "labels_path": getattr(args, "labels", None),
        "dog_threshold": getattr(args, "dog_threshold", None),
    }
    return configfile.resolve_classify(file_values, overrides)


def _resolve_notify(args: argparse.Namespace):
    """Build the NotifyConfig: defaults < [notify] table < CLI flags."""
    config_path = configfile.find_config(getattr(args, "config", None))
    file_values = configfile.load_notify_values(config_path)
    overrides = {"enabled": getattr(args, "notify", None)}
    return configfile.resolve_notify(file_values, overrides)


def _build_notifier(notify_config):
    """Instantiate the Slack notifier, warning (not failing) if misconfigured."""
    from .notify import load_notifier

    try:
        return load_notifier(notify_config)
    except ValueError as exc:
        log.warning("[wan-pulse] notifications disabled: %s", exc)
        return None


def _build_classifier(classify_config):
    """Instantiate the classifier, with a friendly error on missing deps/model."""
    from .classify import load_classifier

    try:
        return load_classifier(classify_config)
    except (ImportError, FileNotFoundError) as exc:
        log.error("[wan-pulse] cannot start classifier: %s", exc)
        return None


def _add_classify_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--model", default=None, help="path to yamnet.tflite")
    p.add_argument("--labels", default=None, help="path to the AudioSet class-map CSV")
    p.add_argument("--dog-threshold", dest="dog_threshold", type=float, default=None,
                   help="score >= this on a dog class => flagged as dog")


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path or configfile.DEFAULT_CONFIG_NAME)
    if path.exists() and not args.force:
        log.info("[wan-pulse] %s already exists (use --force to overwrite)", path)
        return 1
    path.write_text(configfile.template_toml(), encoding="utf-8")
    log.info("[wan-pulse] wrote %s. Edit it, then `wan-pulse run`.", path)
    return 0


def cmd_devices(_args: argparse.Namespace) -> int:
    import sounddevice as sd

    print(sd.query_devices())
    print(f"\nDefault input/output: {sd.default.device}")
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    config = _resolve(args)

    def on_block(level_db: float) -> None:
        over = level_db >= config.threshold_db
        bar_len = int(max(0.0, min(1.0, (level_db + 80.0) / 80.0)) * 40)
        bar = "#" * bar_len
        mark = "  <== over threshold" if over else ""
        sys.stdout.write(f"\r{level_db:7.1f} dBFS |{bar:<40}|{mark}        ")
        sys.stdout.flush()

    # record=False: monitor only shows levels, it never saves or classifies.
    cap = Capture(config, record=False, on_block=on_block)
    log.info("[wan-pulse] monitoring levels (threshold %.1f dBFS). Ctrl+C to stop.",
             config.threshold_db)
    cap.run()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = _resolve(args)
    classify_config = _resolve_classify(args)
    notify_config = _resolve_notify(args)

    classifier = None
    if classify_config.enabled:
        classifier = _build_classifier(classify_config)
        if classifier is None:
            return 1  # deps/model missing; error already printed

    notifier = None
    if notify_config.enabled:
        if classifier is None:
            log.warning("[wan-pulse] notify requires classify; enable [classify] too.")
        else:
            notifier = _build_notifier(notify_config)

    Capture(
        config,
        classifier=classifier,
        classify_config=classify_config,
        notifier=notifier,
        notify_config=notify_config,
    ).run()
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    """Offline: classify existing .wav files (handy for Mac dev without a mic)."""
    import soundfile as sf

    classify_config = _resolve_classify(args, force_enabled=True)
    classifier = _build_classifier(classify_config)
    if classifier is None:
        return 1

    from .writer import SegmentWriter

    for raw in args.paths:
        path = Path(raw)
        audio, sr = sf.read(path, dtype="float32", always_2d=False)
        result = classifier.classify(audio, sr)
        log.info("%s: %s", path.name, result.summary())
        if args.write_sidecar:
            SegmentWriter.write_sidecar(path, {"file": path.name, **result.to_dict()})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wan-pulse",
        description="Record a mic, skip silence, and save only the segments that "
                    "crossed an energy threshold (with pre/post margins) as .wav.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="write a starter wan-pulse.toml")
    p_init.add_argument("path", nargs="?", default=None,
                        help=f"output path (default: {configfile.DEFAULT_CONFIG_NAME})")
    p_init.add_argument("--force", action="store_true", help="overwrite if it exists")
    p_init.set_defaults(func=cmd_init)

    p_dev = sub.add_parser("devices", help="list audio devices")
    p_dev.set_defaults(func=cmd_devices)

    p_mon = sub.add_parser("monitor", help="print live levels to calibrate the threshold")
    _add_audio_args(p_mon)
    _add_log_args(p_mon)
    p_mon.set_defaults(func=cmd_monitor)

    p_run = sub.add_parser("run", help="capture and save bark segments")
    _add_audio_args(p_run)
    p_run.add_argument("--pre-margin", dest="pre_margin", type=float, default=None,
                       help="seconds kept before the trigger")
    p_run.add_argument("--post-margin", dest="post_margin", type=float, default=None,
                       help="silence seconds that close a segment")
    p_run.add_argument("--min-segment", dest="min_segment", type=float, default=None,
                       help="discard segments shorter than this")
    p_run.add_argument("--max-segment", dest="max_segment", type=float, default=None,
                       help="hard cap on a single segment")
    p_run.add_argument("--output-dir", dest="output_dir", default=None,
                       help="where to write .wav files")
    p_run.add_argument("--run-log-dir", dest="run_log_dir", default=None,
                       help="where to write the per-run settings snapshot (run_*.toml)")
    p_run.add_argument("--classify", action=argparse.BooleanOptionalAction, default=None,
                       help="run inference on each saved segment (--no-classify to disable)")
    p_run.add_argument("--notify", action=argparse.BooleanOptionalAction, default=None,
                       help="send a Slack notification on dog detection (--no-notify to disable)")
    _add_classify_args(p_run)
    _add_log_args(p_run)
    p_run.set_defaults(func=cmd_run)

    p_cls = sub.add_parser("classify", help="classify existing .wav files (offline)")
    p_cls.add_argument("paths", nargs="+", help=".wav file(s) to classify")
    p_cls.add_argument("--config", default=None, help="path to TOML config")
    p_cls.add_argument("--write-sidecar", action="store_true",
                       help="also write a .json result next to each .wav")
    _add_classify_args(p_cls)
    _add_log_args(p_cls)
    p_cls.set_defaults(func=cmd_classify)

    return parser


def _configure_logging(args: argparse.Namespace) -> None:
    """File logging for processing commands; console-only for init/devices."""
    raw = getattr(args, "log_file", None)
    want_file = args.command in _FILE_LOG_COMMANDS and raw not in (None, "", "none", "-")
    setup_logging(
        log_file=raw if want_file else None,
        level=getattr(args, "log_level", "INFO"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
