"""Live audio capture: sounddevice stream -> energy gate -> .wav writer.

The sounddevice callback runs on a dedicated audio thread and must stay cheap,
so it does nothing but copy each block onto a queue. The main thread drains the
queue, runs the (lightweight) energy gate, and writes out segments when they
close. This keeps the real-time audio path from ever blocking on disk I/O.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable

import numpy as np

from .classify import Classifier
from .config import CaptureConfig
from .gate import EnergyGate, Segment, rms_dbfs
from .history import HistoryRecorder
from .logsetup import get_logger
from .notify import Notifier
from .writer import SegmentWriter

log = get_logger()


class Capture:
    """Continuous capture loop wiring the mic to the gate and writer."""

    def __init__(
        self,
        config: CaptureConfig,
        *,
        classifier: Classifier | None = None,
        classify_config=None,
        notifier: Notifier | None = None,
        notify_config=None,
        recorder: HistoryRecorder | None = None,
        history_config=None,
        record: bool = True,
        on_segment: Callable[[Segment], None] | None = None,
        on_block: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.gate = EnergyGate(config)
        self.writer = SegmentWriter(config.output_dir)
        self._classifier = classifier
        self._classify_config = classify_config
        self._notifier = notifier
        self._notify_config = notify_config
        self._recorder = recorder
        self._history_config = history_config
        self._record = record
        self._on_segment = on_segment
        self._on_block = on_block
        self._queue: "queue.Queue[np.ndarray]" = queue.Queue()
        # Inference runs on its own thread so heavy ML work never blocks the
        # realtime audio path. Segments are written/announced immediately and
        # classified asynchronously.
        self._classify_jobs: "queue.Queue" = queue.Queue()
        self._classify_thread: threading.Thread | None = None
        self._stop = threading.Event()

    # The sounddevice callback. Keep it minimal: copy + enqueue only.
    def _audio_callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            log.warning("[wan-pulse] audio status: %s", status)
        # indata is reused by PortAudio; we must copy before queueing.
        self._queue.put(indata.copy())

    def _log_device(self, sd) -> None:
        """Log which microphone we're actually capturing from."""
        cfg = self.config
        spec = cfg.device if cfg.device is not None else "default"
        try:
            info = sd.query_devices(cfg.device, "input")
            log.info(
                "[wan-pulse] mic: %s  (device=%s, max in ch %s, native %.0f Hz)",
                info["name"], spec, info["max_input_channels"],
                info["default_samplerate"],
            )
        except Exception as exc:  # noqa: BLE001 - non-fatal, just informational
            log.warning("[wan-pulse] could not query device '%s': %s", spec, exc)

    def run(self) -> None:
        """Open the input stream and process blocks until interrupted."""
        import sounddevice as sd  # imported lazily so tests don't need PortAudio

        cfg = self.config
        self._log_device(sd)
        log.info(
            "[wan-pulse] listening: %s Hz, %s ch, block %.0f ms, threshold %.1f dBFS",
            cfg.samplerate, cfg.channels, cfg.block_ms, cfg.threshold_db,
        )
        if self._record:
            log.info("[wan-pulse] saving segments under ./%s/  (Ctrl+C to stop)", cfg.output_dir)
            if self._classifier is not None:
                backend = getattr(self._classifier, "backend", "")
                suffix = f" ({backend})" if backend else ""
                log.info("[wan-pulse] classifying each segment%s", suffix)
            if self._notifier is not None:
                log.info("[wan-pulse] Slack notifications enabled")
            if self._recorder is not None:
                backend = getattr(self._history_config, "backend", "?")
                log.info("[wan-pulse] recording history (%s)", backend)
            # Snapshot the effective settings so this batch is reproducible.
            from .configfile import write_run_log

            log_path = write_run_log(
                cfg,
                classify=self._classify_config,
                notify=self._notify_config,
                history=self._history_config,
            )
            log.info("[wan-pulse] run settings -> %s", log_path)

        self._start_classifier()
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
            log.info("[wan-pulse] stopping...")
        finally:
            self._drain_and_flush()
            self._shutdown_classifier()

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
        # Save and announce *first* so recording never waits on inference.
        path = self.writer.write(segment)
        log.info(
            "[wan-pulse] saved %s  (%.2fs, peak %.1f dBFS)",
            path.name, segment.duration_sec, segment.peak_dbfs,
        )
        if self._on_segment is not None:
            self._on_segment(segment)
        # Hand inference off to the background worker (no-op if no classifier).
        if self._classifier is not None:
            self._classify_jobs.put((segment, path))

    # --- background inference -------------------------------------------------

    def _start_classifier(self) -> None:
        if self._classifier is None or self._classify_thread is not None:
            return
        self._classify_thread = threading.Thread(
            target=self._classify_worker, name="wan-pulse-classify", daemon=True
        )
        self._classify_thread.start()

    def _classify_worker(self) -> None:
        while True:
            job = self._classify_jobs.get()
            try:
                if job is None:  # sentinel -> drain done, exit
                    return
                segment, path = job
                self._classify_one(segment, path)
            finally:
                self._classify_jobs.task_done()

    def _classify_one(self, segment: Segment, wav_path) -> str:
        """Run inference and drop a JSON sidecar. Never let it break capture."""
        try:
            result = self._classifier.classify(segment.audio, segment.samplerate)
        except Exception as exc:  # noqa: BLE001 - keep recording even if inference fails
            log.error("[wan-pulse] classify failed for %s: %s", wav_path.name, exc)
            return ""
        payload = {
            "file": wav_path.name,
            "duration_sec": round(segment.duration_sec, 3),
            "peak_dbfs": round(segment.peak_dbfs, 2),
            **result.to_dict(),
        }
        self.writer.write_sidecar(wav_path, payload)
        log.info("[wan-pulse] classified %s  -> %s", wav_path.name, result.summary())
        if self._recorder is not None:
            self._recorder.record(result, wav_path, segment)
        if self._notifier is not None:
            self._notifier.notify(result, wav_path, segment)
        return result.summary()

    def _shutdown_classifier(self) -> None:
        if self._classify_thread is None:
            return
        pending = self._classify_jobs.qsize()
        if pending:
            log.info("[wan-pulse] finishing %d pending classification(s)...", pending)
        self._classify_jobs.put(None)  # sentinel after the backlog
        self._classify_thread.join(timeout=30)
        self._classify_thread = None

    def stop(self) -> None:
        self._stop.set()
