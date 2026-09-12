"""Structured (JSON lines) and human-readable logging setup."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import logging
import sys

_STDLIB_LOG_KEYS = frozenset(
    {
        "name",
        "msg",
        "args",
        "created",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "taskName",
        "thread",
        "threadName",
        "exc_info",
        "exc_text",
        "stack_info",
    }
)


class JSONFormatter(logging.Formatter):
    """JSON Lines log formatter (UTC timestamps, ISO-8601 with Z suffix)"""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.datetime.fromtimestamp(record.created, tz=datetime.UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add extra fields
        for key, value in record.__dict__.items():
            if key not in _STDLIB_LOG_KEYS:
                log_data[key] = value

        return json.dumps(log_data, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    """Human-readable log formatter"""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", datefmt="%H:%M:%S"
        )


def setup_logging(level: int, json_output: bool) -> None:
    # Windows GBK consoles cannot encode some log characters; replace instead
    # of raising UnicodeEncodeError mid-log.
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                with contextlib.suppress(Exception):
                    stream.reconfigure(errors="replace")

    handler = logging.StreamHandler(sys.stderr)

    if json_output:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(HumanFormatter())

    logging.root.setLevel(level)
    logging.root.handlers = [handler]

    # Suppress noisy loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def level_from_args(args: argparse.Namespace) -> int:
    if args.quiet:
        return logging.ERROR
    if args.verbose >= 2:
        return logging.DEBUG
    if args.verbose >= 1:
        return logging.INFO
    return logging.WARNING
