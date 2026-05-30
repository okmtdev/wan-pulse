"""Logging setup: send processing messages to both the console and a file.

The rest of the package logs through `get_logger()` (the "wan_pulse" logger).
`setup_logging()` is called once from the CLI to attach a console handler and a
rotating file handler. Keeping this in one place means every `run`/`monitor`/
`classify` session leaves a durable log without each module knowing where it
goes.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "wan_pulse"
DEFAULT_LOG_FILE = "logs/wan-pulse.log"


def get_logger() -> logging.Logger:
    """The package logger. Safe to call at import time; handlers attach later."""
    return logging.getLogger(LOGGER_NAME)


def setup_logging(
    *,
    log_file: str | Path | None = DEFAULT_LOG_FILE,
    level: int | str = logging.INFO,
    console: bool = True,
) -> logging.Logger:
    """Configure the package logger. Idempotent (clears existing handlers)."""
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    logger = get_logger()
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):  # make re-config idempotent
        logger.removeHandler(handler)

    if console:
        console_handler = logging.StreamHandler()
        # Console stays clean; messages already carry the "[wan-pulse]" prefix.
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(console_handler)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
        )
        logger.addHandler(file_handler)
        logger.info("[wan-pulse] logging to %s", path)

    return logger
