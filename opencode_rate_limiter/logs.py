"""Structured (JSON lines) and human-readable logging setup."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import logging
import sys
from pathlib import Path
from typing import Any

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
    """JSON Lines log formatter (UTC timestamps, ISO-8601 with Z suffix).

    The default payload stays lean (timestamp/level/logger/message + extras);
    `verbose=True` adds the code location (module/function/line). Exception
    tracebacks are always attached when the record carries exc_info.
    """

    def __init__(self, verbose: bool = False):
        super().__init__()
        self.verbose = verbose

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": datetime.datetime.fromtimestamp(record.created, tz=datetime.UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if self.verbose:
            log_data["module"] = record.module
            log_data["function"] = record.funcName
            log_data["line"] = record.lineno

        # Add extra fields
        for key, value in record.__dict__.items():
            if key not in _STDLIB_LOG_KEYS:
                log_data[key] = value

        if record.exc_info:
            log_data["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            log_data["stack"] = self.formatStack(record.stack_info)

        try:
            return json.dumps(log_data, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return json.dumps({k: str(v) for k, v in log_data.items()}, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    """Human-readable log formatter"""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", datefmt="%H:%M:%S"
        )


def setup_logging(
    level: int,
    json_output: bool,
    log_file: str | Path | None = None,
    json_verbose: bool = False,
) -> None:
    """Configure root logging: stderr handler plus an optional rotating file.

    `log_file` appends (1MB × 4 files, UTF-8) with the same formatter as
    stderr, so daemon output survives without an external collector.
    """
    from logging.handlers import RotatingFileHandler

    # Windows GBK consoles cannot encode some log characters; replace instead
    # of raising UnicodeEncodeError mid-log.
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                with contextlib.suppress(Exception):
                    stream.reconfigure(errors="replace")

    formatter: logging.Formatter = (
        JSONFormatter(verbose=json_verbose) if json_output else HumanFormatter()
    )

    handlers: list[logging.Handler] = []
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    handlers.append(stderr_handler)

    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    logging.root.setLevel(level)
    logging.root.handlers = handlers

    # Suppress noisy loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def level_from_args(args: argparse.Namespace) -> int:
    if args.quiet:
        return logging.ERROR
    if args.verbose >= 2:
        return logging.DEBUG
    # INFO is the default: daemon lifecycle (start/cycle/rotation) must be
    # visible without flags; -v keeps its historical INFO meaning.
    return logging.INFO
