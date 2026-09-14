"""Tests for the v0.5.0 escape trio: daemon --once, rotate --to, daemon --stop."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from opencode_rate_limiter import (
    AccountPoolConfig,
    Config,
    DaemonConfig,
    ProbeResult,
    cmd_daemon,
    cmd_daemon_stop,
    cmd_rotate,
)


def _pool_cfg() -> Config:
    return Config(
        account_pool=AccountPoolConfig(
            accounts=[
                {"name": "primary", "auth_json": "{}"},
                {"name": "backup1", "auth_json": "{}"},
            ],
            strategy="round_robin",
        )
    )


def _rotate_ns(**over: Any) -> argparse.Namespace:
    base = {"strategy": None, "to": None, "apply": False, "json": True, "dry_run": False}
    base.update(over)
    return argparse.Namespace(**base)


@pytest.mark.asyncio
async def test_rotate_to_picks_named_account(capsys) -> None:
    import json

    rc = await cmd_rotate(_pool_cfg(), _rotate_ns(to="backup1"))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rotated_to"] == "backup1"
    assert payload["explicit"] is True


@pytest.mark.asyncio
async def test_rotate_to_unknown_account_fails(capsys) -> None:
    rc = await cmd_rotate(_pool_cfg(), _rotate_ns(to="nope"))
    assert rc == 1


def _once_daemon(tmp_path: Path, monkeypatch: Any) -> Any:
    from opencode_rate_limiter import RateLimiterDaemon

    daemon = RateLimiterDaemon(
        Config(daemon=DaemonConfig(models=["m1"], probe_timeout_seconds=0.5)),
        state_path=tmp_path / "daemon.json",
        lock_path=tmp_path / "daemon.lock",
    )
    monkeypatch.setattr("opencode_rate_limiter.cli.RateLimiterDaemon", lambda *_a, **_k: daemon)
    return daemon


def _once_ns(**over: Any) -> argparse.Namespace:
    base = {
        "once": True,
        "stop": False,
        "json": True,
        "dry_run": False,
        "interval": None,
        "models": None,
        "config": None,
    }
    base.update(over)
    return argparse.Namespace(**base)


@pytest.mark.asyncio
async def test_daemon_once_exit_zero_when_healthy(tmp_path, monkeypatch, capsys) -> None:
    import json

    daemon = _once_daemon(tmp_path, monkeypatch)

    async def ok(model: str, headers: dict[str, str]) -> ProbeResult:
        return ProbeResult(model=model, status="available", http_status=200, timestamp="t")

    daemon.prober.probe = ok
    rc = await cmd_daemon(Config(), _once_ns())
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["status"] == "available"
    assert not (tmp_path / "daemon.lock").exists()  # lock released
    assert (tmp_path / "daemon.json").exists()  # state persisted


@pytest.mark.asyncio
async def test_daemon_once_exit_one_when_limited(tmp_path, monkeypatch) -> None:
    daemon = _once_daemon(tmp_path, monkeypatch)

    async def limited(model: str, headers: dict[str, str]) -> ProbeResult:
        return ProbeResult(model=model, status="rate_limited", http_status=429, timestamp="t")

    daemon.prober.probe = limited
    rc = await cmd_daemon(Config(), _once_ns())
    assert rc == 1


@pytest.mark.asyncio
async def test_daemon_once_refused_when_locked(tmp_path, monkeypatch) -> None:
    from opencode_rate_limiter import RateLimiterDaemon

    holder = RateLimiterDaemon(Config(), lock_path=tmp_path / "daemon.lock")
    holder._acquire_lock()
    try:
        _once_daemon(tmp_path, monkeypatch)
        rc = await cmd_daemon(Config(), _once_ns())
        assert rc == 1
    finally:
        holder._release_lock()


def _stop_ns(**over: Any) -> argparse.Namespace:
    base = {"json": False}
    base.update(over)
    return argparse.Namespace(**base)


@pytest.mark.asyncio
async def test_stop_without_lock_reports_not_running(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "opencode_rate_limiter.daemon.get_daemon_lock_path",
        lambda: tmp_path / "daemon.lock",
    )
    assert await cmd_daemon_stop(_stop_ns()) == 0
    assert "not appear to be running" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_stop_removes_stale_lock(tmp_path, monkeypatch) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    proc.terminate()
    proc.wait()
    lock = tmp_path / "daemon.lock"
    lock.write_text(str(proc.pid), encoding="utf-8")
    monkeypatch.setattr("opencode_rate_limiter.daemon.get_daemon_lock_path", lambda: lock)
    assert await cmd_daemon_stop(_stop_ns()) == 0
    assert not lock.exists()


@pytest.mark.asyncio
async def test_stop_terminates_live_process(tmp_path, monkeypatch) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lock = tmp_path / "daemon.lock"
        lock.write_text(str(proc.pid), encoding="utf-8")
        monkeypatch.setattr("opencode_rate_limiter.daemon.get_daemon_lock_path", lambda: lock)
        assert await cmd_daemon_stop(_stop_ns()) == 0
        proc.wait(timeout=15)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.asyncio
async def test_stop_refuses_self_lock(tmp_path, monkeypatch, capsys) -> None:
    from opencode_rate_limiter import RateLimiterDaemon

    lock = tmp_path / "daemon.lock"
    holder = RateLimiterDaemon(Config(), lock_path=lock)
    holder._acquire_lock()
    try:
        monkeypatch.setattr("opencode_rate_limiter.daemon.get_daemon_lock_path", lambda: lock)
        assert await cmd_daemon_stop(_stop_ns()) == 1
        assert "refusing" in capsys.readouterr().out
    finally:
        holder._release_lock()
