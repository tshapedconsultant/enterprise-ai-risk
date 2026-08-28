"""
Central logging for the Enterprise AI Risk Console.

Environment:
  LOG_LEVEL   — DEBUG | INFO | WARNING | ERROR (default INFO)
  LOG_FORMAT  — text | json (default text)
  LOG_FILE    — optional path; RotatingFileHandler if set (e.g. logs/app.log)
  LOG_FILE_MAX_BYTES — rotate at this size (default 5 MiB)
  LOG_FILE_BACKUP_COUNT — rotated files to keep (default 5)

Tests use the same module via conftest (LOG_LEVEL=DEBUG, file under tests/logs/).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

_CONFIGURED = False
_ROOT_LOGGER = "enterprise_ai_risk"


class JsonFormatter(logging.Formatter):
    """One JSON object per line for log aggregators."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in (
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "message",
            ):
                continue
            if key in payload:
                continue
            payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging(
    level: Optional[str] = None,
    fmt: Optional[str] = None,
    log_file: Optional[str] = None,
    force: bool = False,
) -> None:
    """Apply handlers to the root enterprise_ai_risk logger (once unless force=True)."""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    level_name = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    log_level = getattr(logging, level_name, logging.INFO)
    format_name = (fmt or os.getenv("LOG_FORMAT", "text")).lower()
    file_path = log_file or os.getenv("LOG_FILE", "").strip()

    root = logging.getLogger(_ROOT_LOGGER)
    root.handlers.clear()
    root.setLevel(log_level)
    root.propagate = False

    if format_name == "json":
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    stream.setLevel(log_level)
    root.addHandler(stream)

    if file_path:
        parent = os.path.dirname(file_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        max_bytes = int(os.getenv("LOG_FILE_MAX_BYTES", str(5 * 1024 * 1024)))
        backups = int(os.getenv("LOG_FILE_BACKUP_COUNT", "5"))
        file_handler = RotatingFileHandler(
            file_path,
            maxBytes=max(1024, max_bytes),
            backupCount=max(1, backups),
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(log_level)
        root.addHandler(file_handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Child logger under enterprise_ai_risk (e.g. enterprise_ai_risk.app.main)."""
    if not name.startswith(_ROOT_LOGGER):
        name = f"{_ROOT_LOGGER}.{name}"
    return logging.getLogger(name)
