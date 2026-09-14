"""Single-instance locking for the probe daemon (pid file + OS file lock)."""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Any

from platformdirs import user_state_dir


def _flock_path(lock_path: Path) -> Path:
    """Sentinel file carrying the held OS lock (kept separate so the pid
    file stays readable while locked — Windows byte-range locks deny
    concurrent opens of the same file)."""
    return lock_path.with_suffix(".flock")


def _flock_nonblocking(fh: Any) -> None:
    """Acquire an exclusive file lock without blocking.

    Raises BlockingIOError when another process holds the lock. Unlike the
    pid-file check, the OS releases this lock on process death, so a held
    lock always means a live holder — no stale-lock race.
    """
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as e:
            raise BlockingIOError(f"lock held by another process: {e}") from e
    else:
        import fcntl

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise BlockingIOError(f"lock held by another process: {e}") from e


def _funlock(fh: Any) -> None:
    """Release a lock acquired with _flock_nonblocking (best effort)."""
    with contextlib.suppress(OSError):
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class DaemonLockError(RuntimeError):
    """Raised when another live daemon instance already holds the lock"""

    def __init__(self, pid: int, lock_path: Path):
        super().__init__(
            f"another daemon instance appears to be running (pid {pid}, lock {lock_path})"
        )
        self.pid = pid
        self.lock_path = lock_path


def get_daemon_lock_path() -> Path:
    """Path to the daemon's single-instance lock file"""
    return Path(user_state_dir("opencode-rate-limiter")) / "daemon.lock"


def _pid_alive(pid: int) -> bool:
    """Check whether a process id is alive (cross-platform, never signals)"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) would TERMINATE the process on Windows, so query via ctypes
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # noqa: N806 - Win32 constant
        STILL_ACTIVE = 259  # noqa: N806 - Win32 constant
        # getattr (not attribute access): ctypes.windll only exists in the
        # win32 stubs, so a plain access fails mypy on Linux CI while a
        # suppression comment is flagged as unused on Windows.
        kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    import errno

    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    return True
