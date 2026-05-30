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
    }
    config = configfile.resolve_config(file_values, overrides)
    if config_path is not None:
        print(f"[wan-pulse] config: {config_path}")
    return config


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path or configfile.DEFAULT_CONFIG_NAME)
    if path.exists() and not args.force:
        print(f"[wan-pulse] {path} already exists (use --force to overwrite)")
        return 1
    path.write_text(configfile.template_toml(), encoding="utf-8")
    print(f"[wan-pulse] wrote {path}. Edit it, then `wan-pulse run`.")
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

    cap = Capture(config, on_block=on_block)
    print(f"[wan-pulse] monitoring levels (threshold {config.threshold_db:.1f} dBFS). Ctrl+C to stop.")
    cap.run()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = _resolve(args)
    Capture(config).run()
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
    p_run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
