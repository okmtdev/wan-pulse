"""Energy-based voice/sound activity gate.

This is the heart of the "skip silence, keep only the loud bits" logic, and it
is deliberately free of any audio I/O so it can be unit-tested with synthetic
arrays. Feed it fixed-size blocks via :meth:`EnergyGate.process`; it returns a
:class:`Segment` whenever a sound region (padded with pre/post margins) closes.

State machine::

    IDLE  --(block RMS >= threshold)-->  ACTIVE
    ACTIVE --(silence >= post_margin, or length >= max)--> emit Segment, IDLE
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from .config import CaptureConfig
from .ring_buffer import RingBuffer

# Floor used so a fully-silent (all-zero) block maps to a very low dB value
# instead of -inf.
_EPS = 1e-10


def rms_dbfs(block: np.ndarray) -> float:
    """Root-mean-square level of a float block, in dBFS.

    Assumes samples are in [-1.0, 1.0] (float32/float64). 0 dBFS is full scale.
    """
    if block.size == 0:
        return -np.inf
    rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))
    return 20.0 * np.log10(max(rms, _EPS))


@dataclass
class Segment:
    """A captured region of audio that crossed the energy threshold."""

    audio: np.ndarray  # shape (frames,) for mono or (frames, channels)
    samplerate: int
    channels: int
    start_time: float  # epoch seconds when the segment began (incl. pre-margin)
    peak_dbfs: float = field(default=-np.inf)

    @property
    def duration_sec(self) -> float:
        return len(self.audio) / float(self.samplerate)


class EnergyGate:
    """Turns a stream of audio blocks into discrete sound segments."""

    IDLE = "idle"
    ACTIVE = "active"

    def __init__(self, config: CaptureConfig, *, clock=time.time) -> None:
        self.config = config
        self._clock = clock

        self._preroll = RingBuffer.from_seconds(
            config.pre_margin_sec, config.samplerate
        )
        self._post_margin_samples = int(
            round(config.post_margin_sec * config.samplerate)
        )
        self._min_samples = int(round(config.min_segment_sec * config.samplerate))
        self._max_samples = int(round(config.max_segment_sec * config.samplerate))

        self.state = self.IDLE
        self._seg_blocks: list[np.ndarray] = []
        self._seg_samples = 0
        self._silence_samples = 0
        self._seg_start_time = 0.0
        self._seg_peak_dbfs = -np.inf

    @property
    def threshold_db(self) -> float:
        return self.config.threshold_db

    def process(self, block: np.ndarray) -> Segment | None:
        """Feed one audio block. Returns a Segment when one closes, else None."""
        level = rms_dbfs(block)
        active = level >= self.config.threshold_db

        if self.state == self.IDLE:
            if active:
                self._start_segment(block, level)
            else:
                # Keep recent silence around so we can prepend the pre-margin
                # once a sound eventually triggers.
                self._preroll.push(block)
            return None

        # ACTIVE
        self._seg_blocks.append(block)
        self._seg_samples += len(block)
        self._seg_peak_dbfs = max(self._seg_peak_dbfs, level)
        if active:
            self._silence_samples = 0
        else:
            self._silence_samples += len(block)

        closed_by_silence = self._silence_samples >= self._post_margin_samples
        closed_by_length = self._seg_samples >= self._max_samples
        if closed_by_silence or closed_by_length:
            return self._finalize()
        return None

    def flush(self) -> Segment | None:
        """Close any in-progress segment (e.g. on shutdown)."""
        if self.state == self.ACTIVE:
            return self._finalize()
        return None

    # --- internal helpers ---

    def _start_segment(self, block: np.ndarray, level: float) -> None:
        # Pre-roll holds the pre-margin (blocks before the trigger); the
        # triggering block itself is appended explicitly so it's never lost,
        # even when pre_margin_sec == 0.
        self._seg_blocks = self._preroll.drain()
        self._seg_blocks.append(block)
        self._seg_samples = sum(len(b) for b in self._seg_blocks)
        self._silence_samples = 0
        self._seg_peak_dbfs = level
        # Back-date the start time by the pre-roll we're carrying.
        self._seg_start_time = self._clock() - (
            self._seg_samples / float(self.config.samplerate)
        )
        self.state = self.ACTIVE

    def _finalize(self) -> Segment | None:
        audio = (
            np.concatenate(self._seg_blocks)
            if self._seg_blocks
            else np.empty((0,), dtype=np.float32)
        )
        seg_samples = self._seg_samples
        start_time = self._seg_start_time
        peak = self._seg_peak_dbfs

        self._reset_to_idle()

        if seg_samples < self._min_samples:
            return None  # too short -> discard as a noise blip
        return Segment(
            audio=audio,
            samplerate=self.config.samplerate,
            channels=self.config.channels,
            start_time=start_time,
            peak_dbfs=peak,
        )

    def _reset_to_idle(self) -> None:
        self.state = self.IDLE
        self._seg_blocks = []
        self._seg_samples = 0
        self._silence_samples = 0
        self._seg_peak_dbfs = -np.inf
        self._preroll.clear()
