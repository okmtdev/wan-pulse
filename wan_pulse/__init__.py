"""wan-pulse: real-time dog-bark capture skeleton.

Pipeline (the parts implemented here are the "always-on / lightweight" stages):

    Mic (M-305)
      -> audio stream (sounddevice, continuous)
      -> energy gate (RMS threshold = "it made a sound")
      -> save the segment to .wav (with pre/post margins)
      -> [later] model inference / notification

This package gives you the working skeleton: record, skip silence, and save
only the segments that crossed the energy threshold, padded with a configurable
margin before and after.
"""

from .config import CaptureConfig
from .gate import EnergyGate, Segment
from .ring_buffer import RingBuffer

__all__ = ["CaptureConfig", "EnergyGate", "Segment", "RingBuffer"]

__version__ = "0.1.0"
