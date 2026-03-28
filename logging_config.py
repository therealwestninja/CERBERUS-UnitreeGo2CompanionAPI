"""
cerberus/utils/logging_config.py  — CERBERUS v3.1
==================================================
Structured logging configuration.

Inspired by unitree/logging-mp for multiprocess-safe, coloured, structured output.
Provides get_logger() factory used across all CERBERUS modules.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from typing import Optional


# ── ANSI colours (terminal only) ────────────────────────────────────────── #
_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_GREY   = "\033[90m"
_CYAN   = "\033[36m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_BRED   = "\033[1;31m"


class _ColouredFormatter(logging.Formatter):
    _LEVEL_COLOURS = {
        logging.DEBUG:    _GREY,
        logging.INFO:     _CYAN,
        logging.WARNING:  _YELLOW,
        logging.ERROR:    _RED,
        logging.CRITICAL: _BRED,
    }

    def format(self, record: logging.LogRecord) -> str:
        colour = self._LEVEL_COLOURS.get(record.levelno, "")
        record.levelname = f"{colour}{_BOLD}{record.levelname:8}{_RESET}"
        record.name      = f"{_GREY}{record.name}{_RESET}"
        return super().format(record)


def configure_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    max_bytes: int = 10 * 1024 * 1024,   # 10 MB
    backup_count: int = 3,
) -> None:
    """
    Configure root logger for CERBERUS.

    Call once at startup (done automatically by the FastAPI server lifespan).

    Args:
        level:        Log level string ("DEBUG", "INFO", "WARNING", "ERROR")
        log_file:     Optional file path for rotating file handler
        max_bytes:    Rotate log file at this size
        backup_count: Keep this many rotated files
    """
    numeric = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric)

    # Remove existing handlers
    root.handlers.clear()

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(numeric)
    if sys.stdout.isatty():
        fmt = _ColouredFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s  %(message)s",
            datefmt="%H:%M:%S",
        )
    else:
        fmt = logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    console.setFormatter(fmt)
    root.addHandler(console)

    # Optional rotating file handler
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        fh.setLevel(numeric)
        fh.setFormatter(logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        ))
        root.addHandler(fh)

    # Suppress noisy third-party loggers
    for noisy in ("uvicorn.access", "websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a named logger (convenience wrapper)."""
    return logging.getLogger(name)
