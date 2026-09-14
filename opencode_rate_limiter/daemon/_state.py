"""Persisted daemon state: status snapshot, cooldowns, budgets, history."""

from __future__ import annotations

import datetime as _dt
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from platformdirs import user_state_dir

from ..prober import ProbeResult


def _now() -> _dt.datetime:
    """Current UTC time (timezone-aware; persisted as ISO-8601)."""
    return _dt.datetime.now(_dt.UTC)


def _parse_cooldown_deadline(raw: Any) -> _dt.datetime | None:
    """Parse a persisted cooldown deadline (aware ISO, trailing Z, or legacy naive)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.UTC)
    return moment.astimezone(_dt.UTC)


def _now_iso() -> str:
    """Current UTC timestamp in ISO-8601 Z format"""
    return _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")


@dataclass
class DaemonStatus:
    """Runtime state snapshot for the daemon"""

    running: bool = False
    started_at: float = 0.0
    last_probe: str = ""
    next_probe: str = ""
    last_cleanup: str = ""
    total_cycles: int = 0
    total_cleanups: int = 0
    model_results: dict[str, ProbeResult] = field(default_factory=dict)
    pool_health: dict[str, dict[str, Any]] = field(default_factory=dict)
    history: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=20))
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=50))

    def to_dict(self) -> dict[str, Any]:

        uptime = 0
        if self.running and self.started_at:
            uptime = int(time.monotonic() - self.started_at)
        return {
            "running": self.running,
            "uptime_seconds": uptime,
            "last_probe": self.last_probe,
            "next_probe": self.next_probe,
            "last_cleanup": self.last_cleanup,
            "total_cycles": self.total_cycles,
            "total_cleanups": self.total_cleanups,
            "models": {name: r.to_dict() for name, r in sorted(self.model_results.items())},
            "pool_health": self.pool_health,
            "history": list(self.history),
            "events": list(self.events),
        }


def get_daemon_state_path() -> Path:
    """Path to the daemon's persisted state file"""
    return Path(user_state_dir("opencode-rate-limiter")) / "daemon.json"


def write_daemon_state(data: dict[str, Any], path: Path | None = None) -> None:
    """Atomically persist daemon state to disk"""
    target = path or get_daemon_state_path()
    log = logging.getLogger("daemon")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(target)
    except OSError as e:
        log.debug("Failed to write daemon state: %s", e)


def load_daemon_state(path: Path | None = None) -> dict[str, Any] | None:
    """Read the daemon's persisted state file, if present"""
    target = path or get_daemon_state_path()
    if not target.exists():
        return None
    try:
        with open(target, encoding="utf-8") as f:
            return cast("dict[str, Any]", json.load(f))
    except (json.JSONDecodeError, OSError) as e:
        logging.getLogger("main").debug("Failed to read daemon state: %s", e)
        return None


__all__ = [
    "DaemonStatus",
    "get_daemon_state_path",
    "load_daemon_state",
    "write_daemon_state",
]
