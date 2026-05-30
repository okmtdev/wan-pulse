"""Tests for the energy gate and ring buffer.

These run entirely on synthetic numpy arrays, so no microphone, PortAudio, or
sounddevice install is required. This is what lets development happen on macOS
(or CI) even though the target hardware is a Raspberry Pi.
"""

from __future__ import annotations

import numpy as np
import pytest

from wan_pulse.config import CaptureConfig
from wan_pulse.gate import EnergyGate, Segment, rms_dbfs
from wan_pulse.ring_buffer import RingBuffer


SR = 16_000
BLOCK = 480  # 30 ms at 16 kHz


def tone(n_blocks: int, amplitude: float) -> list[np.ndarray]:
    """A list of constant-amplitude noise blocks (loud if amplitude high)."""
    rng = np.random.default_rng(0)
    return [
        (rng.standard_normal(BLOCK).astype(np.float32) * amplitude)
        for _ in range(n_blocks)
    ]


def silence(n_blocks: int) -> list[np.ndarray]:
    return [np.zeros(BLOCK, dtype=np.float32) for _ in range(n_blocks)]


def make_gate(**overrides) -> EnergyGate:
    base = dict(
        samplerate=SR,
        block_ms=30.0,
        threshold_db=-40.0,
        pre_margin_sec=0.09,   # ~3 blocks
        post_margin_sec=0.12,  # ~4 blocks
        min_segment_sec=0.0,
        max_segment_sec=10.0,
    )
    base.update(overrides)
    # Deterministic clock so start_time is predictable.
    clock = iter(np.arange(0, 100_000, 1.0))
    return EnergyGate(CaptureConfig(**base), clock=lambda: next(clock))


# --- rms_dbfs ---

def test_rms_dbfs_silence_is_very_low():
    assert rms_dbfs(np.zeros(100, dtype=np.float32)) < -150


def test_rms_dbfs_full_scale_is_zero():
    # Constant +/-1 signal has RMS 1.0 -> 0 dBFS.
    sig = np.ones(100, dtype=np.float32)
    assert rms_dbfs(sig) == pytest.approx(0.0, abs=1e-6)


def test_rms_dbfs_empty_block():
    assert rms_dbfs(np.empty(0)) == -np.inf


# --- RingBuffer ---

def test_ring_buffer_retains_capacity():
    rb = RingBuffer.from_seconds(0.09, SR)  # ~1440 samples ~ 3 blocks
    for b in silence(10):
        rb.push(b)
    # Should retain at least the requested capacity, but not the whole 10 blocks.
    assert rb.samples >= 1440
    assert rb.samples < 10 * BLOCK


def test_ring_buffer_drain_clears():
    rb = RingBuffer(1000)
    for b in silence(5):
        rb.push(b)
    drained = rb.drain()
    assert sum(len(x) for x in drained) > 0
    assert rb.samples == 0


# --- EnergyGate state machine ---

def test_silence_never_emits():
    gate = make_gate()
    emitted = [gate.process(b) for b in silence(50)]
    assert all(s is None for s in emitted)
    assert gate.state == EnergyGate.IDLE


def test_loud_region_emits_one_segment():
    gate = make_gate()
    blocks = silence(5) + tone(10, amplitude=0.3) + silence(10)
    segments = [s for b in blocks if (s := gate.process(b)) is not None]
    assert len(segments) == 1
    seg = segments[0]
    assert isinstance(seg, Segment)
    # Sound was 10 blocks; segment should also include pre + post margins.
    expected_min = 10 * BLOCK
    assert len(seg.audio) > expected_min
    assert gate.state == EnergyGate.IDLE


def test_pre_margin_included():
    gate = make_gate(pre_margin_sec=0.09)  # ~3 blocks
    blocks = silence(5) + tone(5, amplitude=0.3) + silence(10)
    seg = next(s for b in blocks if (s := gate.process(b)) is not None)
    # 5 loud blocks + ~3 pre + ~4 post margin blocks worth of samples.
    assert len(seg.audio) >= (5 + 3) * BLOCK


def test_two_separated_barks_make_two_segments():
    gate = make_gate()
    blocks = (
        silence(3)
        + tone(4, 0.3)
        + silence(10)   # long enough to close first segment
        + tone(4, 0.3)
        + silence(10)
    )
    segments = [s for b in blocks if (s := gate.process(b)) is not None]
    assert len(segments) == 2


def test_short_blip_discarded_by_min_segment():
    gate = make_gate(min_segment_sec=0.5, pre_margin_sec=0.0, post_margin_sec=0.06)
    blocks = silence(3) + tone(1, 0.3) + silence(5)
    segments = [s for b in blocks if (s := gate.process(b)) is not None]
    assert segments == []


def test_max_segment_forces_close():
    gate = make_gate(max_segment_sec=0.15)  # ~5 blocks
    blocks = tone(20, 0.3)
    segments = [s for b in blocks if (s := gate.process(b)) is not None]
    assert len(segments) >= 1
    for seg in segments:
        assert seg.duration_sec <= 0.16 + 1e-6


def test_flush_emits_open_segment():
    gate = make_gate(min_segment_sec=0.0)
    for b in silence(3) + tone(5, 0.3):
        gate.process(b)
    assert gate.state == EnergyGate.ACTIVE
    seg = gate.flush()
    assert seg is not None
    assert gate.state == EnergyGate.IDLE


def test_peak_dbfs_recorded():
    gate = make_gate()
    blocks = silence(3) + tone(6, 0.5) + silence(8)
    seg = next(s for b in blocks if (s := gate.process(b)) is not None)
    assert seg.peak_dbfs > -40.0
