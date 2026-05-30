"""Configuration for the capture pipeline.

All tunables live here so the energy gate and audio I/O can be exercised in
tests without a real microphone. Defaults are chosen to be sane starting points;
use `wan-pulse monitor` to calibrate `threshold_db` for your room/mic.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CaptureConfig:
    """Settings for the record -> gate -> save pipeline."""

    # --- Audio stream ---
    samplerate: int = 16_000
    """Sample rate in Hz. 16 kHz is plenty for bark detection and keeps files
    small; bump it if your downstream model wants more. The M-305 (and most USB
    mics) support 16 kHz, but if the device refuses, try 44100 or 48000."""

    channels: int = 1
    """Number of input channels to capture (1 = mono)."""

    block_ms: float = 30.0
    """Size of each audio block in milliseconds. The energy gate evaluates one
    RMS value per block, so this is also the time resolution of detection."""

    device: int | str | None = None
    """Input device index or (sub)name. None = system default input.
    Use `wan-pulse devices` to list available devices."""

    # --- Energy gate ---
    threshold_db: float = -40.0
    """Activation threshold in dBFS (decibels relative to full scale). A block
    whose RMS level is at or above this is considered "sound". Quiet rooms sit
    around -60 dB; tune with `wan-pulse monitor`."""

    pre_margin_sec: float = 0.5
    """Seconds of audio kept *before* the trigger (from the ring buffer), so the
    onset of the bark isn't clipped."""

    post_margin_sec: float = 0.8
    """Seconds of continuous silence that must elapse after the last sound
    before a segment is closed. Also the amount of trailing audio kept."""

    min_segment_sec: float = 0.3
    """Segments shorter than this (total duration) are discarded as noise."""

    max_segment_sec: float = 15.0
    """Hard cap on a single segment so a continuous noise source can't grow an
    unbounded recording."""

    # --- Output ---
    output_dir: str = "recordings"
    """Directory where .wav segments are written (organized into per-day
    subfolders)."""

    run_log_dir: str = "runs"
    """Directory for the per-run settings snapshots (run_*.toml). Kept separate
    from output_dir so they don't pile up among the recordings."""

    @property
    def blocksize(self) -> int:
        """Number of samples per block (derived from block_ms and samplerate)."""
        return max(1, int(round(self.samplerate * self.block_ms / 1000.0)))

    def __post_init__(self) -> None:
        if self.samplerate <= 0:
            raise ValueError("samplerate must be positive")
        if self.channels < 1:
            raise ValueError("channels must be >= 1")
        if self.block_ms <= 0:
            raise ValueError("block_ms must be positive")
        if self.pre_margin_sec < 0 or self.post_margin_sec < 0:
            raise ValueError("margins must be non-negative")
        if self.min_segment_sec < 0:
            raise ValueError("min_segment_sec must be non-negative")
        if self.max_segment_sec <= 0:
            raise ValueError("max_segment_sec must be positive")


@dataclass
class ClassifyConfig:
    """Settings for the (optional) inference stage that labels each segment.

    Inference is off by default so the core capture skeleton runs without the
    heavy ML dependencies. The same model file and code path run on both macOS
    (dev) and the Raspberry Pi (prod) via a TFLite interpreter.
    """

    enabled: bool = False
    """Run the classifier on each saved segment (online, in `on_segment`)."""

    model_path: str = "models/yamnet.tflite"
    """Path to the YAMNet TFLite model (see scripts/download-yamnet.sh)."""

    labels_path: str = "models/yamnet_class_map.csv"
    """Path to the AudioSet class-map CSV (index,mid,display_name)."""

    dog_threshold: float = 0.3
    """A segment is flagged as a dog when the best dog-class score is >= this."""

    top_k: int = 5
    """How many top labels to keep in the per-segment sidecar JSON."""

    animal_detection: bool = False
    """Also detect generic animal sounds (AudioSet "Animal" / "Domestic animals, pets").
    When True, segments that pass animal_threshold but not dog_threshold are flagged
    as is_animal=True and treated as detections by notify/history."""

    animal_threshold: float = 0.3
    """A segment is flagged as an animal when the best animal-class score is >= this
    (only evaluated when animal_detection = True)."""

    def __post_init__(self) -> None:
        if not 0.0 <= self.dog_threshold <= 1.0:
            raise ValueError("dog_threshold must be in [0, 1]")
        if self.top_k < 1:
            raise ValueError("top_k must be >= 1")
        if not 0.0 <= self.animal_threshold <= 1.0:
            raise ValueError("animal_threshold must be in [0, 1]")


@dataclass
class NotifyConfig:
    """Settings for the (optional) notification stage.

    Posts a message to Slack when a dog is detected. The webhook URL is a
    secret, so it is read from an environment variable rather than the config
    file. Disabled by default.
    """

    enabled: bool = False
    """Send a Slack notification for qualifying segments."""

    webhook_env: str = "WAN_PULSE_SLACK_WEBHOOK"
    """Name of the env var holding the Slack Incoming Webhook URL (text only)."""

    webhook_url: str = ""
    """The Slack webhook URL written directly (overrides webhook_env). Secret:
    redacted in the run-log snapshot. Prefer the env var for shared setups."""

    attach_audio: bool = False
    """Upload the .wav itself (needs a bot token + channel, not a webhook)."""

    bot_token_env: str = "WAN_PULSE_SLACK_BOT_TOKEN"
    """Name of the env var holding the Slack bot token (xoxb-...) for uploads."""

    bot_token: str = ""
    """The Slack bot token written directly (overrides bot_token_env). Secret:
    redacted in the run-log snapshot."""

    channel: str = ""
    """Channel ID (e.g. C0123ABCD) to upload the audio to. Required for uploads."""

    only_dog: bool = True
    """Only notify for segments classified as a dog (skip other noises)."""

    min_dog_score: float = 0.0
    """Extra gate: require the dog score to be at least this to notify."""

    cooldown_sec: float = 30.0
    """Minimum seconds between notifications (avoids spam on continuous barking)."""

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_dog_score <= 1.0:
            raise ValueError("min_dog_score must be in [0, 1]")
        if self.cooldown_sec < 0:
            raise ValueError("cooldown_sec must be non-negative")


@dataclass
class HistoryConfig:
    """Settings for the (optional) detection-history log (spreadsheet / CSV)."""

    enabled: bool = False
    """Append one row per classified segment to the history sink."""

    backend: str = "gsheet"
    """Where to write history: 'gsheet' (Google Sheets via Apps Script web app)
    or 'csv' (a local file)."""

    csv_path: str = "history/detections.csv"
    """Output file when backend = 'csv'."""

    webhook_env: str = "WAN_PULSE_SHEET_WEBHOOK"
    """Env var holding the Apps Script web-app URL when backend = 'gsheet'."""

    webhook_url: str = ""
    """The Apps Script URL written directly (overrides webhook_env). Secret:
    redacted in the run-log snapshot."""

    only_dog: bool = False
    """If true, only log segments classified as a dog (default: log all)."""

    def __post_init__(self) -> None:
        if self.backend not in ("gsheet", "csv"):
            raise ValueError("history backend must be 'gsheet' or 'csv'")


@dataclass
class WebConfig:
    """Settings for the local web app (browse/play/label recorded segments)."""

    enabled: bool = False
    """Start the web app alongside `wan-pulse run`."""

    host: str = "127.0.0.1"
    """Bind address. 127.0.0.1 = local only (safe). Use 0.0.0.0 to expose on LAN."""

    port: int = 8765
    """TCP port for the web app."""

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be in 1..65535")




