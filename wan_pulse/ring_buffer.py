"""A small rolling buffer of recent audio blocks.

The gate is normally idle (and lightweight), but we still need the audio that
came *just before* a bark so the onset isn't clipped. The ring buffer keeps the
last `capacity_samples` worth of mono/stereo blocks; when a trigger fires we
drain it into the front of the new segment.
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np


class RingBuffer:
    """Keeps roughly the last N samples of audio as a deque of blocks.

    Blocks may have varying lengths; the buffer trims whole blocks from the
    oldest end once the retained sample count exceeds the capacity.
    """

    def __init__(self, capacity_samples: int) -> None:
        if capacity_samples < 0:
            raise ValueError("capacity_samples must be non-negative")
        self._capacity = capacity_samples
        self._blocks: deque[np.ndarray] = deque()
        self._samples = 0

    @classmethod
    def from_seconds(cls, seconds: float, samplerate: int) -> "RingBuffer":
        return cls(int(math.ceil(max(0.0, seconds) * samplerate)))

    @property
    def samples(self) -> int:
        """Number of samples currently retained."""
        return self._samples

    def push(self, block: np.ndarray) -> None:
        """Append a block and trim the oldest blocks past capacity."""
        self._blocks.append(block)
        self._samples += len(block)
        while self._blocks and self._samples - len(self._blocks[0]) >= self._capacity:
            # Drop the oldest block only if we'd still have >= capacity without it,
            # so we never under-fill the requested pre-roll.
            dropped = self._blocks.popleft()
            self._samples -= len(dropped)

    def drain(self) -> list[np.ndarray]:
        """Return all retained blocks and clear the buffer."""
        blocks = list(self._blocks)
        self._blocks.clear()
        self._samples = 0
        return blocks

    def clear(self) -> None:
        self._blocks.clear()
        self._samples = 0
