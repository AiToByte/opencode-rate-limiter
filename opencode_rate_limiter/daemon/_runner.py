"""Probe daemon runner: periodic probing, auto-cleanup/rotation, signals."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import json
import logging
import os
import random
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from queue import Queue
from typing import Any

from ..cleanup import CleanupManager
from ..config import Config, ProberConfig
from ..headers import HeaderInjector
from ..paths import get_opencode_version
from ..pool import Account, AccountPool
from ..prober import ModelProber, ProbeResult
from ._lock import (
    DaemonLockError,
    _flock_nonblocking,
    _flock_path,
    _funlock,
    _pid_alive,
    get_daemon_lock_path,
)
from ._state import (
    DaemonStatus,
    _now,
    _now_iso,
    _parse_cooldown_deadline,
    load_daemon_state,
    write_daemon_state,
)


def _make_signal_bridge(
    loop: asyncio.AbstractEventLoop, callback: Callable[[], None]
) -> Callable[[int, Any], None]:
    """Bridge a stdlib signal handler into the running asyncio loop"""

    def _bridge(signum: int, frame: Any) -> None:
        loop.call_soon_threadsafe(callback)

    return _bridge


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
        self._lock_fh: Any = None
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
        self._budget_cursor = 0
        self._notify_queue: Queue[dict[str, Any]] = Queue()
        self._notify_worker_started = False
        self._notify_worker_lock = threading.Lock()
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
        old_prober: ModelProber | None = getattr(self, "prober", None)
        old_client = None
        old_settings: tuple[float, ProberConfig] | None = None
        if old_prober is not None and not old_prober._batch_active:
            # Detach the warm client; ownership moves to the new prober when
            # the probe settings are unchanged, else it is closed below.
            old_client, old_prober._shared_client = old_prober._shared_client, None
            old_settings = (old_prober.timeout, old_prober.config)
        elif old_prober is not None:
            # A probe batch is in flight (SIGHUP raced a cycle): leave the
            # old client with the old prober rather than breaking the flight.
            self.log.debug("Rebuild during active probe batch; old client left in place")
        version = get_opencode_version()
        self.injector = HeaderInjector(self.config.headers, version)
        self.headers = self.injector.build_headers()
        self.prober = ModelProber(self.config.daemon.probe_timeout_seconds, self.config.prober)
        if old_client is not None:
            if old_settings == (self.prober.timeout, self.prober.config):
                self.prober._shared_client = old_client
                old_client = None
            else:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    loop.create_task(old_client.aclose())
                # Without a running loop (e.g. interpreter teardown) the
                # detached client is left to the garbage collector.
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
        """Append a decision event to the audit ring and dispatch notifications"""
        event = {"ts": _now_iso(), "kind": kind, **fields}
        self.status.events.append(event)
        if self.config.daemon.notify_webhook or self.config.daemon.notify_command:
            # Fire-and-forget through one daemon worker: delivery never
            # blocks the loop, never piles up threads, and never keeps the
            # process alive at teardown.
            with self._notify_worker_lock:
                if not self._notify_worker_started:
                    threading.Thread(
                        target=self._notify_worker, daemon=True, name="notify-worker"
                    ).start()
                    self._notify_worker_started = True
            self._notify_queue.put(dict(event))

    def _notify_worker(self) -> None:
        while True:
            event = self._notify_queue.get()
            try:
                self._notify_sync(event)
            except Exception as e:  # defensive: the worker must never die
                self.log.debug("Notify worker error: %s", e)
            finally:
                self._notify_queue.task_done()

    def _notify_sync(self, event: dict[str, Any]) -> None:
        """Dispatch one event to the configured webhook / command hook.

        Every failure is downgraded to a warning: a broken notification
        channel must never affect the probe loop.
        """
        webhook = self.config.daemon.notify_webhook
        if webhook:
            try:
                import httpx

                resp = httpx.post(
                    webhook,
                    json={"source": "opencode-rate-limiter", "event": event},
                    timeout=5.0,
                )
                if resp.status_code >= 300:
                    self.log.warning("Notify webhook returned %s", resp.status_code)
            except Exception as e:
                self.log.warning("Notify webhook failed: %s", e)

        command = self.config.daemon.notify_command
        if command:
            try:
                proc = subprocess.run(
                    command,
                    shell=True,
                    input=json.dumps(event, ensure_ascii=False),
                    capture_output=True,
                    text=True,
                    timeout=10.0,
                )
                if proc.returncode != 0:
                    self.log.warning(
                        "Notify command exited %s: %s", proc.returncode, proc.stderr.strip()
                    )
            except Exception as e:
                self.log.warning("Notify command failed: %s", e)

    def _load_runtime_state(self) -> None:
        """Restore runtime state across restarts: probe budget + cooldowns"""
        state = load_daemon_state(self._state_path)
        usage = state.get("probe_usage") if state else None
        if isinstance(usage, dict):
            self._probe_day = str(usage.get("day", ""))
            with contextlib.suppress(TypeError, ValueError):
                self._probe_count = int(usage.get("count", 0))
        cooldowns = state.get("cooldowns") if state else None
        if isinstance(cooldowns, dict):
            now_utc = _dt.datetime.now(_dt.UTC)
            now_mono = time.monotonic()
            for model, raw in cooldowns.items():
                deadline = _parse_cooldown_deadline(raw)
                if deadline is None:
                    continue
                remaining = (deadline - now_utc).total_seconds()
                if remaining > 0:
                    self._cooldowns[str(model)] = now_mono + remaining

    async def run(self) -> None:

        self._load_runtime_state()
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
                # Small jitter (±5%, capped at ±30s) so identically
                # configured instances do not hammer the gateway in lockstep.
                jitter = random.uniform(-0.05, 0.05) * wait_seconds
                jitter = max(-30.0, min(30.0, jitter))
                await self._wait(max(1, int(wait_seconds + jitter)))
        finally:
            self.status.running = False
            self._running = False
            self._restore_signal_handlers()
            self._persist_state()
            self._release_lock()
            await self.prober.aclose()
            self.log.info(
                "Daemon stopped",
                extra={
                    "cycles": self.status.total_cycles,
                    "cleanups": self.status.total_cleanups,
                },
            )

    def _acquire_lock(self) -> None:
        """Take the single-instance lock and record our pid.

        Mutual exclusion comes from the OS file lock held on the sentinel
        file for the process lifetime (released by the OS itself on death,
        so a held lock always means a live holder — no stale-lock race).
        The pid file keeps its informational role and stays readable.
        """
        lock_path = self._lock_path or get_daemon_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        sentinel = _flock_path(lock_path)
        # Lifetime-held handle (released in _release_lock): SIM115 does not apply.
        fh = open(sentinel, "a+b")  # noqa: SIM115
        try:
            _flock_nonblocking(fh)
        except BlockingIOError:
            fh.close()
            existing = self._read_lock_pid(lock_path)
            if existing is not None and _pid_alive(existing):
                raise DaemonLockError(existing, lock_path) from None
            # Locked but the recorded pid is dead/garbled: some live holder
            # exists (the OS would have released a dead holder's lock), so
            # refuse rather than corrupt its state file.
            raise DaemonLockError(existing or 0, lock_path) from None
        # We hold the lock: a stale pid (dead process, corrupt content) can
        # be safely overwritten — no live process can be using this file.
        existing = self._read_lock_pid(lock_path)
        if existing is not None and existing != os.getpid():
            self.log.warning("Taking over stale daemon lock (pid %s)", existing)
        lock_path.write_text(str(os.getpid()), encoding="utf-8")
        self._lock_file = lock_path
        self._lock_fh = fh

    @staticmethod
    def _read_lock_pid(lock_path: Path) -> int | None:
        try:
            return int(lock_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _release_lock(self) -> None:
        """Unlock, close and remove the lock files only if we still own them"""
        fh = self._lock_fh
        self._lock_fh = None
        if fh is not None:
            with contextlib.suppress(OSError):
                _funlock(fh)
            with contextlib.suppress(OSError):
                fh.close()
        if self._lock_file is None:
            return
        if self._read_lock_pid(self._lock_file) == os.getpid():
            with contextlib.suppress(OSError):
                self._lock_file.unlink()
            with contextlib.suppress(OSError):
                _flock_path(self._lock_file).unlink()
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
            account = self.pool.get_next(record=False)
            if account is None:
                return None
            if self._key_cooldowns.get(account.name, 0) <= now:
                self.pool.note_served(account)
                return account
        return None

    async def _probe_cycle(self) -> list[ProbeResult]:

        now = time.monotonic()
        models = self._effective_models()
        self.status.total_cycles += 1
        self.status.last_probe = _now_iso()
        # The pooled client lives across cycles (opened once, closed at
        # shutdown) so keep-alive connections survive between probes.
        self.prober.open()

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
            return []
        if budget:
            remaining = budget - self._probe_count
            if len(active) > remaining:
                # Rotate the slice start each trimmed cycle so tail models
                # are not starved by a fixed head-first truncation.
                start = self._budget_cursor % len(active)
                rotated = active[start:] + active[:start]
                trimmed = rotated[:remaining]
                self._budget_cursor = (start + len(trimmed)) % len(active)
                self.log.info(
                    "Trimming probe batch to daily budget",
                    extra={"models": len(active), "remaining": remaining},
                )
                active = trimmed
            else:
                self._budget_cursor = 0

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
            # Transient / reasoning-replay / upstream failures are NOT
            # credential failures (pool.mark_result skips them); they only
            # feed the backoff counter and audit events below.
            if r.status != "rate_limited":
                name = account_by_model.get(r.model)
                if name and self.pool is not None:
                    self.pool.mark_result(
                        name,
                        success=r.status == "available",
                        latency_ms=r.latency_ms,
                        error_type=r.error_type,
                        error_kind=r.error_kind,
                    )
            kind = r.error_kind or ""
            if r.status == "error" and kind == "transient_transport":
                self._record_event("transient_skipped", model=r.model, error=r.error or "")
            elif r.status == "error" and kind == "reasoning_replay":
                self._record_event("reasoning_replay", model=r.model, error=r.error or "")
                self.log.warning(
                    "Reasoning-replay failure (poisoned session, not a limit)",
                    extra={"model": r.model, "error": r.error or ""},
                )
            elif r.status == "error" and kind == "upstream":
                self._record_event("upstream_error", model=r.model, error=r.error or "")

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
        # Clear cooldowns only for models that probed fine: a transient or
        # upstream error must not lift a valid rate-limit cooldown.
        for r in results:
            if r.status == "available":
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
        return results

    async def run_once(self) -> list[ProbeResult]:
        """Run a single probe cycle and exit (cron / Task Scheduler friendly).

        Takes the single-instance lock so a one-shot run never races a live
        daemon over the state file; exit-code mapping is the caller's job.
        """
        self._load_runtime_state()
        self._acquire_lock()
        try:
            return await self._probe_cycle()
        finally:
            self._persist_state()
            self._release_lock()
            await self.prober.aclose()

    def _backoff_multiplier(self) -> int:
        """Exponential wait multiplier after consecutive all-error cycles (cap 8x)"""
        return 1 << min(self._error_streak, 3)

    def _persist_state(self) -> None:
        """Write the current status snapshot to the state file"""
        data = self.status.to_dict()
        data["probe_usage"] = {"day": self._probe_day, "count": self._probe_count}
        if self._cooldowns:
            # Absolute UTC timestamps: cooldowns survive daemon restarts
            now = time.monotonic()
            data["cooldowns"] = {
                m: (_now() + _dt.timedelta(seconds=until - now)).isoformat()
                for m, until in sorted(self._cooldowns.items())
                if until > now
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

        # Mark the account that actually served this model as failing and
        # rotate away from it. The rotation itself happens lazily: the next
        # probe cycle picks a fresh account (consuming the round-robin cursor
        # exactly once per pick), so no extra get_next() here.
        if self.pool is not None:
            served = self.pool.last_served
            name = account_name or (served.name if served is not None else None)
            if name is not None:
                self.pool.mark_result(
                    name,
                    success=False,
                    latency_ms=result.latency_ms,
                    error_type=result.error_type,
                )
            if result.error_type == "RateLimitError" and name is not None:
                # Key-dimension limit: cool this account down (configurable).
                cooldown_s = self.config.daemon.key_cooldown_seconds
                if cooldown_s > 0:
                    self._key_cooldowns[name] = time.monotonic() + cooldown_s
                    self._record_event("key_cooldown", account=name, seconds=cooldown_s)
                    self.log.info(
                        "Key cooldown armed",
                        extra={
                            "account": name,
                            "seconds": cooldown_s,
                            "error_type": result.error_type,
                        },
                    )
            if len(self.pool.accounts) > 1 and name is not None:
                self._record_event(
                    "rotation",
                    model=result.model,
                    **{"from": name},
                    reason="rate_limited",
                )
                self.log.info(
                    "Account rotated away",
                    extra={
                        "from": name,
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
