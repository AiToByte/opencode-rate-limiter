"""Probe daemon: periodic probing, auto-cleanup/rotation, signal control,
state persistence and single-instance locking.

Split into focused modules; this package re-exports the public surface so
`from opencode_rate_limiter.daemon import ...` keeps working.
"""

from __future__ import annotations

from ._lock import (
    DaemonLockError,
    _flock_nonblocking,
    _flock_path,
    _funlock,
    _pid_alive,
    get_daemon_lock_path,
)
from ._runner import RateLimiterDaemon
from ._state import (
    DaemonStatus,
    _now,
    _now_iso,
    _parse_cooldown_deadline,
    get_daemon_state_path,
    load_daemon_state,
    write_daemon_state,
)

__all__ = [
    "DaemonLockError",
    "DaemonStatus",
    "RateLimiterDaemon",
    "_flock_nonblocking",
    "_flock_path",
    "_funlock",
    "_now",
    "_now_iso",
    "_parse_cooldown_deadline",
    "_pid_alive",
    "get_daemon_lock_path",
    "get_daemon_state_path",
    "load_daemon_state",
    "write_daemon_state",
]
