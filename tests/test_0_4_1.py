"""Regression tests for the v0.4.1 patch round (A-track leftovers)."""

from __future__ import annotations

import pytest

from opencode_rate_limiter import Config


def test_merge_recurses_into_nested_tables() -> None:
    base = Config()
    merged = Config._merge(base, {"account_pool": {"score_weights": {"success": 0.6}}})
    assert merged.account_pool.score_weights["success"] == 0.6
    # untouched siblings survive a partial nested override
    assert merged.account_pool.score_weights["latency"] == 0.3
    assert merged.account_pool.score_weights["recency"] == 0.2
    # base object not mutated
    assert base.account_pool.score_weights["success"] == 0.5


def test_merge_matches_fields_case_insensitively() -> None:
    base = Config()
    merged = Config._merge(base, {"DAEMON": {"Interval_Seconds": 120}})
    assert merged.daemon.interval_seconds == 120


def test_dry_run_counts_would_clear() -> None:
    from opencode_rate_limiter import CleanupConfig
    from opencode_rate_limiter.cleanup import CleanupManager

    mgr = CleanupManager(CleanupConfig())
    result = mgr.full_cleanup(dry_run=True)
    assert result.cleared_count == 0
    assert result.would_clear_count >= 0
    assert "would_clear_count" in result.to_dict()


def test_error_body_size_cap() -> None:
    import httpx

    from opencode_rate_limiter.prober import _parse_error_type

    big = httpx.Response(
        429,
        headers={"Content-Length": str(10 * 1024 * 1024)},
        json={"error": {"type": "FreeUsageLimitError"}},
    )
    assert _parse_error_type(big) is None
    small = httpx.Response(429, json={"error": {"type": "FreeUsageLimitError"}})
    assert _parse_error_type(small) == "FreeUsageLimitError"


@pytest.mark.asyncio
async def test_probe_all_overlap_raises() -> None:
    from opencode_rate_limiter import ModelProber

    prober = ModelProber(10.0)
    prober._batch_active = True
    try:
        with pytest.raises(RuntimeError, match="must not overlap"):
            await prober.probe_all(["m1"], {})
    finally:
        prober._batch_active = False


@pytest.mark.asyncio
async def test_persistent_client_reused_across_batches(httpx_mock) -> None:
    from opencode_rate_limiter import ModelProber

    for _ in range(2):
        httpx_mock.add_response(url=ModelProber.ZEN_ENDPOINT, status_code=200, json={})
    prober = ModelProber(10.0)
    prober.open()
    try:
        first = await prober.probe_all(["m1"], {})
        second = await prober.probe_all(["m1"], {})
        assert all(r.status == "available" for r in (*first, *second))
        assert prober._shared_client is not None  # still open for reuse
    finally:
        await prober.aclose()
    assert prober._shared_client is None


def test_no_proxy_helper() -> None:
    from opencode_rate_limiter.diagnostics import endpoint_bypassed_by_no_proxy

    endpoint = "https://opencode.ai/zen/v1/chat/completions"
    assert endpoint_bypassed_by_no_proxy(endpoint, None) is False
    assert endpoint_bypassed_by_no_proxy(endpoint, "") is False
    assert endpoint_bypassed_by_no_proxy(endpoint, "*") is True
    assert endpoint_bypassed_by_no_proxy(endpoint, "opencode.ai") is True
    assert endpoint_bypassed_by_no_proxy(endpoint, "example.com") is False
