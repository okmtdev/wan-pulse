"""Command-line entry point for wan-pulse.

Subcommands:
  devices  list available audio input/output devices
  monitor  print live RMS levels (use this to calibrate --threshold-db)
  run      capture, skip silence, and save bark segments to .wav
"""

from __future__ import annotations

import argparse
import sys

from .capture import Capture
from .config import CaptureConfig


def _add_audio_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--samplerate", type=int, default=CaptureConfig.samplerate,
                   help="sample rate in Hz (default: %(default)s)")
    p.add_argument("--channels", type=int, default=CaptureConfig.channels,
                   help="input channels (default: %(default)s)")
    p.add_argument("--block-ms", type=float, default=CaptureConfig.block_ms,
                   help="block size in ms / detection resolution (default: %(default)s)")
    p.add_argument("--device", default=None,
                   help="input device index or name substring (default: system default)")
    p.add_argument("--threshold-db", type=float, default=CaptureConfig.threshold_db,
                   help="activation threshold in dBFS (default: %(default)s)")


def _config_from_args(args: argparse.Namespace) -> CaptureConfig:
    device: int | str | None = args.device
    if isinstance(device, str) and device.isdigit():
        device = int(device)
    return CaptureConfig(
        samplerate=args.samplerate,
        channels=args.channels,
        block_ms=args.block_ms,
        device=device,
        threshold_db=args.threshold_db,
        pre_margin_sec=getattr(args, "pre_margin", CaptureConfig.pre_margin_sec),
        post_margin_sec=getattr(args, "post_margin", CaptureConfig.post_margin_sec),
        min_segment_sec=getattr(args, "min_segment", CaptureConfig.min_segment_sec),
        max_segment_sec=getattr(args, "max_segment", CaptureConfig.max_segment_sec),
        output_dir=getattr(args, "output_dir", CaptureConfig.output_dir),
    )


def cmd_devices(_args: argparse.Namespace) -> int:
    import sounddevice as sd

    print(sd.query_devices())
    print(f"\nDefault input/output: {sd.default.device}")
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    config = _config_from_args(args)

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
    config = _config_from_args(args)
    Capture(config).run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wan-pulse",
        description="Record a mic, skip silence, and save only the segments that "
                    "crossed an energy threshold (with pre/post margins) as .wav.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_dev = sub.add_parser("devices", help="list audio devices")
    p_dev.set_defaults(func=cmd_devices)

    p_mon = sub.add_parser("monitor", help="print live levels to calibrate the threshold")
    _add_audio_args(p_mon)
    p_mon.set_defaults(func=cmd_monitor)

    p_run = sub.add_parser("run", help="capture and save bark segments")
    _add_audio_args(p_run)
    p_run.add_argument("--pre-margin", dest="pre_margin", type=float,
                       default=CaptureConfig.pre_margin_sec,
                       help="seconds kept before the trigger (default: %(default)s)")
    p_run.add_argument("--post-margin", dest="post_margin", type=float,
                       default=CaptureConfig.post_margin_sec,
                       help="silence seconds that close a segment (default: %(default)s)")
    p_run.add_argument("--min-segment", dest="min_segment", type=float,
                       default=CaptureConfig.min_segment_sec,
                       help="discard segments shorter than this (default: %(default)s)")
    p_run.add_argument("--max-segment", dest="max_segment", type=float,
                       default=CaptureConfig.max_segment_sec,
                       help="hard cap on a single segment (default: %(default)s)")
    p_run.add_argument("--output-dir", dest="output_dir", default=CaptureConfig.output_dir,
                       help="where to write .wav files (default: %(default)s)")
    p_run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
