"""Live audio capture: sounddevice stream -> energy gate -> .wav writer.

The sounddevice callback runs on a dedicated audio thread and must stay cheap,
so it does nothing but copy each block onto a queue. The main thread drains the
queue, runs the (lightweight) energy gate, and writes out segments when they
close. This keeps the real-time audio path from ever blocking on disk I/O.
"""

from __future__ import annotations

import queue
import sys
import threading
from typing import Callable

import numpy as np

from .classify import Classifier
from .config import CaptureConfig
from .gate import EnergyGate, Segment, rms_dbfs
from .writer import SegmentWriter


class Capture:
    """Continuous capture loop wiring the mic to the gate and writer."""

    def __init__(
        self,
        config: CaptureConfig,
        *,
        classifier: Classifier | None = None,
        classify_config=None,
        record: bool = True,
        on_segment: Callable[[Segment], None] | None = None,
        on_block: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.gate = EnergyGate(config)
        self.writer = SegmentWriter(config.output_dir)
        self._classifier = classifier
        self._classify_config = classify_config
        self._record = record
        self._on_segment = on_segment
        self._on_block = on_block
        self._queue: "queue.Queue[np.ndarray]" = queue.Queue()
        self._stop = threading.Event()

    # The sounddevice callback. Keep it minimal: copy + enqueue only.
    def _audio_callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            print(f"[audio] {status}", file=sys.stderr)
        # indata is reused by PortAudio; we must copy before queueing.
        self._queue.put(indata.copy())

    def run(self) -> None:
        """Open the input stream and process blocks until interrupted."""
        import sounddevice as sd  # imported lazily so tests don't need PortAudio

        cfg = self.config
        print(
            f"[wan-pulse] listening: {cfg.samplerate} Hz, {cfg.channels} ch, "
            f"block {cfg.block_ms:.0f} ms, threshold {cfg.threshold_db:.1f} dBFS"
        )
        if self._record:
            print(f"[wan-pulse] saving segments under ./{cfg.output_dir}/  (Ctrl+C to stop)")
            if self._classifier is not None:
                backend = getattr(self._classifier, "backend", "")
                print(f"[wan-pulse] classifying each segment ({backend})".replace(" ()", ""))
            # Snapshot the effective settings so this batch is reproducible.
            from .configfile import write_run_log

            log_path = write_run_log(cfg, classify=self._classify_config)
            print(f"[wan-pulse] run settings -> {log_path}")

        stream = sd.InputStream(
            samplerate=cfg.samplerate,
            blocksize=cfg.blocksize,
            channels=cfg.channels,
            dtype="float32",
            device=cfg.device,
            callback=self._audio_callback,
        )
        try:
            with stream:
                self._consume_loop()
        except KeyboardInterrupt:
            print("\n[wan-pulse] stopping...")
        finally:
            self._drain_and_flush()

    def _consume_loop(self) -> None:
        while not self._stop.is_set():
            try:
                block = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._handle_block(block)

    def _drain_and_flush(self) -> None:
        # Process whatever is still queued, then close any open segment.
        while True:
            try:
                block = self._queue.get_nowait()
            except queue.Empty:
                break
            self._handle_block(block)
        self._emit(self.gate.flush())

    def _handle_block(self, block: np.ndarray) -> None:
        if self._on_block is not None:
            self._on_block(rms_dbfs(block))
        if self._record:
            self._emit(self.gate.process(block))

    def _emit(self, segment: Segment | None) -> None:
        if segment is None:
            return
        path = self.writer.write(segment)
        line = (
            f"[wan-pulse] saved {path.name}  "
            f"({segment.duration_sec:.2f}s, peak {segment.peak_dbfs:.1f} dBFS)"
        )
        if self._classifier is not None:
            label = self._classify_segment(segment, path)
            if label:
                line += f"  -> {label}"
        print(line)
        if self._on_segment is not None:
            self._on_segment(segment)

    def _classify_segment(self, segment: Segment, wav_path) -> str:
        """Run inference and drop a JSON sidecar. Never let it break capture."""
        try:
            result = self._classifier.classify(segment.audio, segment.samplerate)
        except Exception as exc:  # noqa: BLE001 - keep recording even if inference fails
            print(f"[wan-pulse] classify failed: {exc}", file=sys.stderr)
            return ""
        payload = {
            "file": wav_path.name,
            "duration_sec": round(segment.duration_sec, 3),
            "peak_dbfs": round(segment.peak_dbfs, 2),
            **result.to_dict(),
        }
        self.writer.write_sidecar(wav_path, payload)
        return result.summary()

    def stop(self) -> None:
        self._stop.set()
