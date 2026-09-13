"""Probe daemon: periodic probing, 429 auto-cleanup/rotation, signal control,
state persistence and single-instance locking."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import json
import logging
import os
import signal
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from platformdirs import user_state_dir

from .cleanup import CleanupManager
from .config import Config
from .headers import HeaderInjector
from .paths import get_opencode_version
from .pool import Account, AccountPool
from .prober import ModelProber, ProbeResult


def _now_iso() -> str:
    """Current UTC timestamp in ISO-8601 Z format"""
    return _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")


def _make_signal_bridge(
    loop: asyncio.AbstractEventLoop, callback: Callable[[], None]
) -> Callable[[int, Any], None]:
    """Bridge a stdlib signal handler into the running asyncio loop"""

    def _bridge(signum: int, frame: Any) -> None:
        loop.call_soon_threadsafe(callback)

    return _bridge


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


class RateLimiterDaemon:
    """Periodic probe daemon with auto-cleanup, account rotation and signal control"""

    def __init__(
        self,
        config: Config,
        config_path: Path | None = None,
        interval_override: int | None = None,
        models_override: list[str] | None = None,
        state_path: Path | None = None,
        lock_path: Path | None = None,
    ):
        self.config = config
        self.config_path = config_path
        self._interval_override = interval_override
        self._models_override = models_override
        self._state_path = state_path
        self._lock_path = lock_path
        self._lock_file: Path | None = None
        self.log = logging.getLogger("daemon")
        self.status = DaemonStatus()
        self._running = False
        self._stop_event = asyncio.Event()
        self._probe_event = asyncio.Event()
        self._error_streak = 0
        self._cooldowns: dict[str, float] = {}  # model -> monotonic deadline
        self._key_cooldowns: dict[str, float] = {}  # account name -> monotonic deadline
        self._probe_day = ""
        self._probe_count = 0
        self._prev_handlers: dict[int, Any] = {}
        self._signal_fallback_sigs: list[int] = []
        self._rebuild()

    def _effective_interval(self) -> int:
        return (
            self._interval_override
            if self._interval_override is not None
            else self.config.daemon.interval_seconds
        )

    def _effective_models(self) -> list[str]:
        if self._models_override:
            return self._models_override
        return self.config.daemon.models

    def _rebuild(self) -> None:
        """(Re)build runtime components from current config"""
        version = get_opencode_version()
        self.injector = HeaderInjector(self.config.headers, version)
        self.headers = self.injector.build_headers()
        self.prober = ModelProber(self.config.daemon.probe_timeout_seconds, self.config.prober)
        self.cleanup = CleanupManager(self.config.cleanup)
        self.pool = (
            AccountPool(self.config.account_pool) if self.config.account_pool.accounts else None
        )
        # (Re)size the probe-history ring buffer to the configured window
        size = self.config.daemon.history_size
        if self.status.history.maxlen != size:
            self.status.history = deque(self.status.history, maxlen=size)
        event_size = self.config.daemon.event_history_size
        if self.status.events.maxlen != event_size:
            self.status.events = deque(self.status.events, maxlen=event_size)

    def _record_event(self, kind: str, **fields: Any) -> None:
        """Append a decision event to the audit ring (persisted via daemon.json)"""
        self.status.events.append({"ts": _now_iso(), "kind": kind, **fields})

    def _load_probe_usage(self) -> None:
        """Restore the daily probe budget counter across restarts"""
        state = load_daemon_state(self._state_path)
        usage = state.get("probe_usage") if state else None
        if isinstance(usage, dict):
            self._probe_day = str(usage.get("day", ""))
            with contextlib.suppress(TypeError, ValueError):
                self._probe_count = int(usage.get("count", 0))

    async def run(self) -> None:

        self._load_probe_usage()
        self._acquire_lock()
        self._install_signal_handlers()
        self._running = True
        self.status.running = True
        self.status.started_at = time.monotonic()
        interval = self._effective_interval()
        self.log.info(
            "Daemon started",
            extra={"interval": interval, "models": len(self._effective_models())},
        )
        try:
            while self._running:
                await self._probe_cycle()
                if not self._running:
                    break
                wait_seconds = interval * self._backoff_multiplier()
                if wait_seconds > interval:
                    self.log.warning(
                        "All probes errored; backing off",
                        extra={"wait_seconds": wait_seconds, "error_streak": self._error_streak},
                    )
                await self._wait(wait_seconds)
        finally:
            self.status.running = False
            self._running = False
            self._restore_signal_handlers()
            self._persist_state()
            self._release_lock()
            self.log.info(
                "Daemon stopped",
                extra={
                    "cycles": self.status.total_cycles,
                    "cleanups": self.status.total_cleanups,
                },
            )

    def _acquire_lock(self) -> None:
        """Create the single-instance lock file, taking over stale locks"""
        lock_path = self._lock_path or get_daemon_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = self._read_lock_pid(lock_path)
            if existing is not None and _pid_alive(existing):
                raise DaemonLockError(existing, lock_path) from None
            # Stale lock from a dead process - take over
            self.log.warning("Removing stale daemon lock (pid %s)", existing)
            with contextlib.suppress(OSError):
                lock_path.unlink()
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        self._lock_file = lock_path

    @staticmethod
    def _read_lock_pid(lock_path: Path) -> int | None:
        try:
            return int(lock_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _release_lock(self) -> None:
        """Remove the lock file only if we still own it"""
        if self._lock_file is None:
            return
        if self._read_lock_pid(self._lock_file) == os.getpid():
            with contextlib.suppress(OSError):
                self._lock_file.unlink()
        self._lock_file = None

    async def _wait(self, interval: int) -> None:
        """Wait for the next probe, interrupted by stop or forced-probe signals"""
        stop_task = asyncio.create_task(self._stop_event.wait())
        probe_task = asyncio.create_task(self._probe_event.wait())
        try:
            done, _ = await asyncio.wait(
                {stop_task, probe_task}, timeout=interval, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (stop_task, probe_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stop_task, probe_task, return_exceptions=True)
        if stop_task in done:
            self._running = False
        if self._probe_event.is_set():
            self._probe_event.clear()

    def _pick_account(self) -> Account | None:
        """Pick the next account, skipping accounts in key-dimension cooldown.

        Returns None when the pool is empty or every account is cooling down
        (probes then fall back to anonymous headers).
        """
        if self.pool is None:
            return None
        now = time.monotonic()
        for _ in range(len(self.pool.accounts)):
            account = self.pool.get_next()
            if account is None:
                return None
            if self._key_cooldowns.get(account.name, 0) <= now:
                return account
        return None

    async def _probe_cycle(self) -> None:

        now = time.monotonic()
        models = self._effective_models()
        self.status.total_cycles += 1
        self.status.last_probe = _now_iso()

        # Per-model rate-limit cooldown: skip models whose cooldown window has
        # not elapsed yet to avoid burning quota on known-limited models.
        skip: dict[str, float] = {}
        if self.config.daemon.respect_cooldown:
            skip = {m: until for m, until in self._cooldowns.items() if until > now}
        active = [m for m in models if m not in skip]
        if skip:
            self.log.info(
                "Skipping models in rate-limit cooldown",
                extra={
                    "models": sorted(skip),
                    "cooldown_seconds": {m: round(until - now) for m, until in skip.items()},
                },
            )

        # Daily probe budget: every probe consumes the same per-IP free quota
        # as real usage, so cap total probe requests per UTC day.
        today = _dt.datetime.now(_dt.UTC).strftime("%Y%m%d")
        if today != self._probe_day:
            self._probe_day = today
            self._probe_count = 0
        budget = self.config.daemon.daily_probe_budget
        if budget and self._probe_count >= budget:
            self.log.warning(
                "Daily probe budget exhausted; skipping cycle",
                extra={"budget": budget, "used": self._probe_count},
            )
            self._record_event("budget_exhausted", budget=budget, used=self._probe_count)
            self._persist_state()
            return
        if budget:
            remaining = budget - self._probe_count
            if len(active) > remaining:
                self.log.info(
                    "Trimming probe batch to daily budget",
                    extra={"models": len(active), "remaining": remaining},
                )
                active = active[:remaining]

        self.log.info("Starting probe cycle", extra={"models": len(active)})

        # Per-model account rotation: pick an account per model and inject its
        # auth token into that probe's headers when resolvable.
        headers_by_model: dict[str, dict[str, str]] = {}
        account_by_model: dict[str, str] = {}
        if self.pool is not None:
            for model in active:
                account = self._pick_account()
                if account is None:
                    continue
                account_by_model[model] = account.name
                token = self.pool.resolve_token(account)
                if token:
                    headers_by_model[model] = self.injector.build_headers(token=token)

        results = await self.prober.probe_all(active, self.headers, headers_by_model or None)
        self._probe_count += len(results)

        for r in results:
            self.status.model_results[r.model] = r
            self.log.info(
                "Probe completed",
                extra={
                    "model": r.model,
                    "status": r.status,
                    "http_status": r.http_status,
                    "latency_ms": round(r.latency_ms, 1),
                },
            )
            if r.retry_after:
                self.log.warning(
                    "Rate limit window", extra={"model": r.model, "retry_after": r.retry_after}
                )
            # Feed results back into health tracking; rate_limited is marked as
            # a failure inside _handle_rate_limited (which also rotates).
            if r.status != "rate_limited":
                name = account_by_model.get(r.model)
                if name and self.pool is not None:
                    self.pool.mark_result(
                        name,
                        success=r.status == "available",
                        latency_ms=r.latency_ms,
                        error_type=r.error_type,
                    )

        limited = [r for r in results if r.status == "rate_limited"]
        for r in limited:
            await self._handle_rate_limited(r, account_by_model.get(r.model))

        # One full cleanup per cycle at most, even when several models are
        # rate-limited in the same pass
        if limited and self.config.daemon.auto_cleanup_on_429:
            await asyncio.to_thread(self.cleanup.full_cleanup)
            self.status.total_cleanups += 1
            self.status.last_cleanup = _now_iso()
            self._record_event("cleanup", trigger="429", models=len(limited))
            self.log.info(
                "Auto cleanup triggered", extra={"trigger": "429", "models": len(limited)}
            )

        # Arm cooldowns for rate-limited models (retry_after, else estimate)
        if self.config.daemon.respect_cooldown:
            for r in limited:
                wait = r.retry_after or r.estimated_reset
                if wait:
                    self._cooldowns[r.model] = time.monotonic() + wait
                    self._record_event("cooldown_armed", model=r.model, seconds=wait)
        # Clear cooldowns for models that probed fine
        for r in results:
            if r.status != "rate_limited":
                self._cooldowns.pop(r.model, None)

        # Backoff: every probe in the cycle errored (network/endpoint trouble)
        if results and all(r.status == "error" for r in results):
            self._error_streak += 1
        else:
            self._error_streak = 0

        # Snapshot account health for the persisted state file
        if self.pool is not None:
            weights = self.config.account_pool.score_weights
            self.status.pool_health = {
                name: {
                    "success": h.success_count,
                    "total": h.total_count,
                    "consecutive_failures": h.consecutive_failures,
                    "avg_latency_ms": round(h.avg_latency_ms, 1),
                    "score": round(h.calculate_score(weights), 3),
                }
                for name, h in sorted(self.pool.health.items())
            }

        # Ring-buffer entry for trend analysis (check command)
        self.status.history.append(
            {"ts": self.status.last_probe, "models": {r.model: r.status for r in results}}
        )

        next_ts = _dt.datetime.now(_dt.UTC) + _dt.timedelta(
            seconds=self._effective_interval() * self._backoff_multiplier()
        )
        self.status.next_probe = next_ts.isoformat().replace("+00:00", "Z")
        self._persist_state()

    def _backoff_multiplier(self) -> int:
        """Exponential wait multiplier after consecutive all-error cycles (cap 8x)"""
        return 1 << min(self._error_streak, 3)

    def _persist_state(self) -> None:
        """Write the current status snapshot to the state file"""
        data = self.status.to_dict()
        data["probe_usage"] = {"day": self._probe_day, "count": self._probe_count}
        if self._cooldowns:
            now = time.monotonic()
            data["cooldowns"] = {
                m: round(until - now) for m, until in sorted(self._cooldowns.items()) if until > now
            }
        data["pid"] = os.getpid()
        data["updated_at"] = _now_iso()
        write_daemon_state(data, self._state_path)

    async def _handle_rate_limited(
        self, result: ProbeResult, account_name: str | None = None
    ) -> None:
        self.log.warning(
            "Rate limited detected",
            extra={
                "model": result.model,
                "retry_after": result.retry_after,
                "estimated_reset": result.estimated_reset,
            },
        )

        # Mark the account that actually served this model as failing, then
        # rotate away from it (when pool has 2+)
        if self.pool is not None:
            current = self.pool.get_current()
            name = account_name or (current.name if current is not None else None)
            if name is not None:
                self.pool.mark_result(
                    name,
                    success=False,
                    latency_ms=result.latency_ms,
                    error_type=result.error_type,
                )
            if result.error_type == "RateLimitError" and name is not None:
                # Key-dimension limit: cool this account down for a minute
                self._key_cooldowns[name] = time.monotonic() + 60
                self._record_event("key_cooldown", account=name, seconds=60)
                self.log.info(
                    "Key cooldown armed",
                    extra={"account": name, "seconds": 60, "error_type": result.error_type},
                )
            if len(self.pool.accounts) > 1:
                next_account = self.pool.get_next()
                if next_account is not None:
                    self._record_event(
                        "rotation",
                        model=result.model,
                        **{"from": name},
                        to=next_account.name,
                    )
                    self.log.info(
                        "Account rotated",
                        extra={
                            "from": name,
                            "to": next_account.name,
                            "strategy": self.config.account_pool.strategy,
                        },
                    )

        # Note: the auto-cleanup itself runs once per probe cycle (see
        # _probe_cycle), not once per rate-limited model.

    def _install_signal_handlers(self) -> None:

        loop = asyncio.get_running_loop()
        mapping: dict[str, Callable[[], None]] = {
            "SIGTERM": self._request_stop,
            "SIGINT": self._request_stop,
            "SIGHUP": self._reload_config,
            "SIGUSR1": self._request_probe,
            "SIGUSR2": self._request_status,
        }
        for name, callback in mapping.items():
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, callback)
            except NotImplementedError:
                previous = signal.getsignal(sig)
                self._prev_handlers[int(sig)] = previous
                self._signal_fallback_sigs.append(int(sig))
                signal.signal(sig, _make_signal_bridge(loop, callback))
            except RuntimeError:
                self.log.debug("Signal handler install skipped for %s", name)

    def _restore_signal_handlers(self) -> None:

        for sig_int in self._signal_fallback_sigs:
            previous = self._prev_handlers.get(sig_int)
            if previous is None:
                continue
            with contextlib.suppress(OSError, ValueError):
                signal.signal(sig_int, previous)
        self._signal_fallback_sigs = []
        self._prev_handlers = {}

    def _request_stop(self) -> None:
        self.log.info("Stopping daemon (graceful)")
        self._running = False
        self._stop_event.set()

    def _request_probe(self) -> None:
        self._probe_event.set()

    def _request_status(self) -> None:
        self.log.info("Daemon status", extra=self.status.to_dict())

    def _reload_config(self) -> None:
        self.log.info("Reloading config (SIGHUP)")
        try:
            new_config = Config.load(self.config_path)
        except Exception as e:
            self.log.error("Config reload failed: %s", e)
            return
        old_pool = self.pool
        # CLI overrides survive the reload
        new_config.daemon.interval_seconds = self._effective_interval()
        if self._models_override:
            new_config.daemon.models = list(self._models_override)
        self.config = new_config
        self._rebuild()
        # Carry account health across the rebuild so strategy decisions and
        # the persisted pool_health snapshot survive a reload
        if self.pool is not None and old_pool is not None:
            for name, health in old_pool.health.items():
                if name in self.pool.health:
                    self.pool.health[name] = health
        self._record_event(
            "reload",
            interval=self._effective_interval(),
            models=len(self._effective_models()),
        )
        self.log.info(
            "Config reloaded",
            extra={"interval": self._effective_interval(), "models": len(self._effective_models())},
        )


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
