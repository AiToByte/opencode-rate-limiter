"""Regression tests for the v0.4.0 correctness + quality pass."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

import opencode_rate_limiter
from opencode_rate_limiter import Config
from opencode_rate_limiter.parser import build_parser
from opencode_rate_limiter.prober import _parse_retry_after


def _pool_config(accounts: list[dict[str, Any]], strategy: str = "round_robin") -> Any:
    from opencode_rate_limiter import AccountPoolConfig

    return AccountPoolConfig(accounts=accounts, strategy=strategy)  # type: ignore[arg-type]


def test_last_served_tracks_picks() -> None:
    from opencode_rate_limiter import AccountPool

    pool = AccountPool(
        _pool_config([{"name": "a", "auth_path": "/a"}, {"name": "b", "auth_path": "/b"}])
    )
    assert pool.last_served is None
    cur = pool.get_current()
    assert cur is not None and cur.name == "a"  # fallback
    first = pool.get_next()
    assert first is not None and pool.last_served is not None
    assert pool.last_served.name == first.name == "a"
    cur = pool.get_current()
    assert cur is not None and cur.name == "a"
    # peek-style picks do not pollute attribution
    pool.get_next(record=False)
    assert pool.last_served is not None and pool.last_served.name == "a"
    pool.note_served(pool.accounts[1])
    assert pool.last_served is not None and pool.last_served.name == "b"


def test_flock_second_holder_refused(tmp_path: Path) -> None:
    from opencode_rate_limiter import Config, RateLimiterDaemon
    from opencode_rate_limiter.daemon import DaemonLockError

    lock = tmp_path / "daemon.lock"
    first = RateLimiterDaemon(Config(), state_path=tmp_path / "s1.json", lock_path=lock)
    first._acquire_lock()
    try:
        second = RateLimiterDaemon(Config(), state_path=tmp_path / "s2.json", lock_path=lock)
        with pytest.raises(DaemonLockError):
            second._acquire_lock()
    finally:
        first._release_lock()
    assert not lock.exists()


def test_kind_validation_rejects_unknown() -> None:
    from opencode_rate_limiter import AccountPoolConfig

    with pytest.raises(ValueError, match="kind"):
        AccountPoolConfig(accounts=[{"name": "a", "auth_path": "/a", "kind": "saml"}]).validate()


def test_public_all_matches_exports() -> None:
    missing = [
        name for name in opencode_rate_limiter.__all__ if not hasattr(opencode_rate_limiter, name)
    ]
    assert missing == []
    assert "cmd_diagnose" in opencode_rate_limiter.__all__
    assert "#" not in opencode_rate_limiter.__all__


def test_retry_after_parses_float_and_date() -> None:
    assert _parse_retry_after("120") == 120
    assert _parse_retry_after("1.9") == 1
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("not-a-date") is None
    import datetime as _dt
    from email.utils import format_datetime

    future = _dt.datetime.now(_dt.UTC) + _dt.timedelta(seconds=90)
    parsed = _parse_retry_after(format_datetime(future))
    assert parsed is not None and 0 <= parsed <= 120


def test_merge_does_not_mutate_base() -> None:
    base = Config()
    before = base.daemon.interval_seconds
    Config._merge(base, {"daemon": {"interval_seconds": 123}})
    assert base.daemon.interval_seconds == before


def test_rotate_strategy_defaults_to_config() -> None:
    parser = build_parser()
    args = parser.parse_args(["rotate"])
    assert args.strategy is None


def test_headers_config_rejects_bad_placeholder() -> None:
    from opencode_rate_limiter import HeadersConfig

    HeadersConfig(user_agent="opencode/{version}").validate()
    with pytest.raises(ValueError, match="unsupported placeholder"):
        HeadersConfig(user_agent="opencode/{bogus}").validate()


def test_latency_score_clamped() -> None:
    from opencode_rate_limiter import AccountHealth

    fresh = AccountHealth(name="fresh", avg_latency_ms=0.0)
    assert fresh.calculate_score() <= 1.0
    slow = AccountHealth(name="slow", success_count=0, total_count=1, avg_latency_ms=100_000.0)
    assert slow.calculate_score() >= 0.0


def test_cleanup_resolves_custom_dirs(tmp_path, monkeypatch) -> None:
    from opencode_rate_limiter import CleanupConfig
    from opencode_rate_limiter.cleanup import CleanupManager

    custom = tmp_path / "mycache"
    custom.mkdir()
    monkeypatch.setenv("T41_CACHE", str(custom))
    mgr = CleanupManager(CleanupConfig(cache_dirs=["$T41_CACHE"]))
    resolved = mgr.resolve_cache_dirs()
    assert custom in resolved


def test_powershell_completion_generates() -> None:
    from opencode_rate_limiter import generate_completions

    script = generate_completions("powershell")
    assert "Register-ArgumentCompleter" in script


@pytest.mark.asyncio
async def test_rotate_keeps_config_strategy_when_flag_absent(monkeypatch, capsys) -> None:
    from opencode_rate_limiter import AccountPoolConfig, cmd_rotate

    cfg = Config()
    cfg.account_pool = AccountPoolConfig(
        accounts=[{"name": "a", "auth_path": "/a.json"}],
        strategy="round_robin",
    )
    args = argparse.Namespace(strategy=None, apply=False, json=True, dry_run=False)
    await cmd_rotate(cfg, args)
    assert cfg.account_pool.strategy == "round_robin"
    out = capsys.readouterr().out
    assert '"strategy": "round_robin"' in out
