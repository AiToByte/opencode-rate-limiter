"""Phase 2 module tests - HeaderInjector, ModelProber, AccountPool, CleanupManager."""

import json
from typing import Any

import pytest

from opencode_rate_limiter import (
    AccountHealth,
    AccountPool,
    AccountPoolConfig,
    CleanupConfig,
    CleanupManager,
    CleanupResult,
    Config,
    HeaderInjector,
    HeadersConfig,
    ModelProber,
    ProberConfig,
    ProbeResult,
    extract_access_token,
)

# =============================================================================
# HeaderInjector Tests
# =============================================================================


class TestHeaderInjector:
    def test_build_headers_default(self):
        injector = HeaderInjector(HeadersConfig(), "1.18.16")
        h = injector.build_headers()
        assert h["User-Agent"] == "opencode/1.18.16"
        assert h["x-opencode-client"] == "opencode-cli"
        assert h["x-opencode-version"] == "1.18.16"
        assert h["Content-Type"] == "application/json"
        assert h["Accept"] == "text/event-stream"

    def test_build_headers_unknown_version(self):
        injector = HeaderInjector(HeadersConfig(), "unknown")
        h = injector.build_headers()
        assert h["User-Agent"] == "opencode/unknown"

    def test_build_headers_with_model(self):
        injector = HeaderInjector(HeadersConfig(), "1.18.16")
        h = injector.build_headers("deepseek-v4-flash-free")
        assert h["x-model"] == "deepseek-v4-flash-free"

    def test_build_headers_custom_template(self):
        cfg = HeadersConfig(user_agent="custom/{version}", x_opencode_version="v{version}")
        injector = HeaderInjector(cfg, "2.0.0")
        h = injector.build_headers()
        assert h["User-Agent"] == "custom/2.0.0"
        assert h["x-opencode-version"] == "v2.0.0"

    def test_to_env_export(self):
        injector = HeaderInjector(HeadersConfig(), "1.18.16")
        export = injector.to_env_export()
        assert 'export USER_AGENT="opencode/1.18.16"' in export
        assert 'export X_OPENCODE_CLIENT="opencode-cli"' in export
        assert 'export CONTENT_TYPE="application/json"' in export

    def test_to_curl_args(self):
        injector = HeaderInjector(HeadersConfig(), "1.18.16")
        args = injector.to_curl_args()
        assert '-H "User-Agent: opencode/1.18.16"' in args
        assert '-H "x-opencode-client: opencode-cli"' in args


# =============================================================================
# ProbeResult Tests
# =============================================================================


class TestProbeResult:
    def test_to_dict(self):
        r = ProbeResult(
            model="test",
            status="available",
            http_status=200,
            latency_ms=45.2,
            timestamp="2026-01-01T00:00:00Z",
        )
        d = r.to_dict()
        assert d["model"] == "test"
        assert d["status"] == "available"
        assert d["http_status"] == 200
        assert d["latency_ms"] == 45.2

    def test_to_dict_with_error(self):
        r = ProbeResult(
            model="test",
            status="error",
            error="timeout",
        )
        d = r.to_dict()
        assert d["error"] == "timeout"
        assert d["http_status"] is None


# =============================================================================
# ModelProber Tests (using respx for HTTP mocking)
# =============================================================================


class TestModelProber:
    @pytest.mark.asyncio
    async def test_probe_available(self, httpx_mock):
        httpx_mock.add_response(
            url=ModelProber.ZEN_ENDPOINT,
            json={"choices": [{"message": {"content": "pong"}}]},
            status_code=200,
        )
        prober = ModelProber(10.0)
        result = await prober.probe("deepseek-v4-flash-free", {"User-Agent": "test"})
        assert result.status == "available"
        assert result.http_status == 200
        assert result.latency_ms > 0

    @pytest.mark.asyncio
    async def test_probe_rate_limited(self, httpx_mock):
        httpx_mock.add_response(
            url=ModelProber.ZEN_ENDPOINT,
            json={"error": {"type": "FreeUsageLimitError"}},
            status_code=429,
            headers={"Retry-After": "120"},
        )
        prober = ModelProber(10.0)
        result = await prober.probe("deepseek-v4-flash-free", {})
        assert result.status == "rate_limited"
        assert result.http_status == 429
        assert result.retry_after == 120
        assert result.estimated_reset == 120

    @pytest.mark.asyncio
    async def test_probe_silent_limit(self, httpx_mock):
        httpx_mock.add_response(
            url=ModelProber.ZEN_ENDPOINT,
            json={"error": {"type": "FreeUsageLimitError", "message": "Rate limit exceeded"}},
            status_code=429,
        )
        prober = ModelProber(10.0)
        result = await prober.probe("deepseek-v4-flash-free", {})
        assert result.status == "rate_limited"
        assert result.retry_after is None
        assert result.estimated_reset == 60

    @pytest.mark.asyncio
    async def test_probe_timeout(self, httpx_mock):
        import httpx as _httpx

        httpx_mock.add_exception(_httpx.TimeoutException("timeout"))
        prober = ModelProber(10.0)
        result = await prober.probe("model", {})
        assert result.status == "error"
        assert result.error == "timeout"

    @pytest.mark.asyncio
    async def test_probe_connection_error(self, httpx_mock):
        import httpx as _httpx

        httpx_mock.add_exception(_httpx.ConnectError("refused"))
        prober = ModelProber(10.0)
        result = await prober.probe("model", {})
        assert result.status == "error"
        assert result.error is not None and "refused" in result.error

    @pytest.mark.asyncio
    async def test_probe_all_concurrent(self, httpx_mock):
        for _ in range(3):
            httpx_mock.add_response(
                url=ModelProber.ZEN_ENDPOINT,
                json={"choices": []},
                status_code=200,
            )
        prober = ModelProber(10.0)
        results = await prober.probe_all(["a", "b", "c"], {})
        assert len(results) == 3
        assert all(r.status == "available" for r in results)

    def test_estimate_reset_with_value(self):
        assert ModelProber._estimate_reset(120) == 120

    def test_estimate_reset_without_value(self):
        assert ModelProber._estimate_reset(None) == 60


# =============================================================================
# AccountHealth Tests
# =============================================================================


class TestAccountHealth:
    def test_success_rate_empty(self):
        h = AccountHealth(name="test")
        assert h.success_rate == 0.0

    def test_success_rate_partial(self):
        h = AccountHealth(name="test", success_count=7, total_count=10)
        assert h.success_rate == 0.7

    def test_calculate_score_healthy(self):
        import time

        h = AccountHealth(
            name="test",
            success_count=95,
            total_count=100,
            avg_latency_ms=100,
            last_error_time=0,
            last_success_time=time.time(),
        )
        score = h.calculate_score()
        assert score > 0.8

    def test_calculate_score_unhealthy(self):
        import time

        h = AccountHealth(
            name="test",
            success_count=10,
            total_count=100,
            avg_latency_ms=2000,
            last_error_time=time.time(),
            last_success_time=0,
        )
        score = h.calculate_score()
        assert score < 0.3


# =============================================================================
# AccountPool Tests
# =============================================================================


class TestAccountPool:
    def _make_pool(
        self, accounts: list[dict[str, str]], strategy: str = "round_robin"
    ) -> AccountPool:
        config = AccountPoolConfig(
            accounts=accounts,
            strategy=strategy,  # type: ignore[arg-type]
        )
        return AccountPool(config)

    def test_empty_pool(self):
        pool = self._make_pool([])
        assert pool.get_next() is None

    def test_round_robin(self):
        pool = self._make_pool(
            [
                {"name": "a", "auth_path": "/a.json"},
                {"name": "b", "auth_path": "/b.json"},
            ]
        )
        first = pool.get_next()
        assert first is not None and first.name == "a"
        second = pool.get_next()
        assert second is not None and second.name == "b"
        third = pool.get_next()
        assert third is not None and third.name == "a"  # Wraps around

    def test_least_used(self):
        pool = self._make_pool(
            [
                {"name": "a", "auth_path": "/a.json"},
                {"name": "b", "auth_path": "/b.json"},
            ],
            strategy="least_used",
        )
        pool.health["a"].total_count = 5
        pool.health["b"].total_count = 2
        least = pool.get_next()
        assert least is not None and least.name == "b"  # Less used

    def test_healthiest(self):
        import time

        pool = self._make_pool(
            [
                {"name": "a", "auth_path": "/a.json"},
                {"name": "b", "auth_path": "/b.json"},
            ],
            strategy="health",
        )
        pool.health["a"].success_count = 10
        pool.health["a"].total_count = 10
        pool.health["a"].avg_latency_ms = 100
        pool.health["b"].success_count = 5
        pool.health["b"].total_count = 10
        pool.health["b"].avg_latency_ms = 100
        pool.health["b"].last_error_time = time.time()
        healthiest = pool.get_next()
        assert healthiest is not None and healthiest.name == "a"  # Healthier

    def test_mark_result_success(self):
        pool = self._make_pool([{"name": "a", "auth_path": "/a.json"}])
        pool.mark_result("a", success=True, latency_ms=50)
        assert pool.health["a"].success_count == 1
        assert pool.health["a"].total_count == 1
        assert pool.health["a"].consecutive_failures == 0

    def test_mark_result_failure(self):
        pool = self._make_pool([{"name": "a", "auth_path": "/a.json"}])
        pool.mark_result("a", success=False)
        assert pool.health["a"].success_count == 0
        assert pool.health["a"].total_count == 1
        assert pool.health["a"].consecutive_failures == 1
        assert pool.health["a"].last_error_time > 0

    def test_read_auth_from_path(self, tmp_path):
        auth_file = tmp_path / "auth.json"
        auth_file.write_text('{"access_token": "test123"}')
        pool = self._make_pool([{"name": "a", "auth_path": str(auth_file)}])
        data = pool.read_auth(pool.accounts[0])
        assert data is not None and data["access_token"] == "test123"

    def test_read_auth_from_env(self, monkeypatch):
        monkeypatch.setenv("TEST_AUTH", '{"access_token": "env123"}')
        pool = self._make_pool([{"name": "a", "env_var": "TEST_AUTH"}])
        data = pool.read_auth(pool.accounts[0])
        assert data is not None and data["access_token"] == "env123"

    def test_read_auth_from_json_string(self):
        pool = self._make_pool([{"name": "a", "auth_json": '{"access_token": "inline123"}'}])
        data = pool.read_auth(pool.accounts[0])
        assert data is not None and data["access_token"] == "inline123"

    def test_read_auth_nonexistent(self):
        pool = self._make_pool([{"name": "a", "auth_path": "/nonexistent/auth.json"}])
        data = pool.read_auth(pool.accounts[0])
        assert data is None


# =============================================================================
# CleanupResult Tests
# =============================================================================


class TestCleanupResult:
    def test_to_dict(self):
        r = CleanupResult(
            cleared_count=3,
            errors=["err1"],
            details=["detail1", "detail2"],
        )
        d = r.to_dict()
        assert d["cleared_count"] == 3
        assert d["errors"] == ["err1"]
        assert len(d["details"]) == 2


# =============================================================================
# CleanupManager Tests
# =============================================================================


class TestCleanupManager:
    def test_reset_rate_limit_state_dry_run(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text('{"rate_limited_until": 123}')
        config = CleanupConfig(state_files=[str(state_file)])
        mgr = CleanupManager(config)
        result = mgr.reset_rate_limit_state(dry_run=True)
        assert result.cleared_count >= 0

    def test_rotate_auth_tokens_backup(self, tmp_path):
        auth_file = tmp_path / "auth.json"
        auth_file.write_text('{"access_token": "old_token", "rate_limited_until": 123}')
        config = CleanupConfig()
        mgr = CleanupManager(config)
        result = mgr.rotate_auth_tokens([auth_file], dry_run=False)
        assert result.cleared_count == 1
        assert (tmp_path / "auth.json.bak").exists()
        data = json.loads(auth_file.read_text())
        assert data["access_token"] == ""
        assert "rate_limited_until" not in data

    def test_purge_cache(self, tmp_path):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "test.json").write_text("{}")
        config = CleanupConfig()
        mgr = CleanupManager(config)
        result = mgr.purge_cache([cache_dir], dry_run=False)
        assert result.cleared_count == 1
        assert cache_dir.exists()
        assert not (cache_dir / "test.json").exists()

    def test_full_cleanup(self, tmp_path):
        auth_file = tmp_path / "auth.json"
        auth_file.write_text('{"access_token": "old"}')
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        cleanup_config = CleanupConfig()
        mgr = CleanupManager(cleanup_config)
        result = mgr.full_cleanup(dry_run=False)
        assert result.cleared_count >= 0


# =============================================================================
# Iteration tests: account auth wired into probe requests
# =============================================================================


class TestExtractAccessToken:
    def test_top_level_token(self):
        assert extract_access_token({"access_token": "tok"}) == "tok"

    def test_nested_token(self):
        auth = {"opencode": {"access_token": "nested-tok"}, "other": 1}
        assert extract_access_token(auth) == "nested-tok"

    def test_missing_token(self):
        assert extract_access_token({"foo": "bar"}) is None

    def test_empty_token_treated_as_missing(self):
        assert extract_access_token({"access_token": ""}) is None


class TestResolveToken:
    def test_resolve_from_auth_path(self, tmp_path):
        auth_file = tmp_path / "auth.json"
        auth_file.write_text('{"access_token": "file-tok"}')
        config = AccountPoolConfig(accounts=[{"name": "a", "auth_path": str(auth_file)}])
        pool = AccountPool(config)
        assert pool.resolve_token(pool.accounts[0]) == "file-tok"

    def test_resolve_missing_source(self):
        config = AccountPoolConfig(accounts=[{"name": "a", "auth_path": "/nonexistent.json"}])
        pool = AccountPool(config)
        assert pool.resolve_token(pool.accounts[0]) is None


class TestHeaderInjectorAuth:
    def test_build_headers_with_token(self):
        injector = HeaderInjector(HeadersConfig(), "1.18.16")
        h = injector.build_headers(token="secret")
        assert h["Authorization"] == "Bearer secret"

    def test_build_headers_without_token(self):
        injector = HeaderInjector(HeadersConfig(), "1.18.16")
        h = injector.build_headers()
        assert "Authorization" not in h


class TestCmdProbeAuth:
    def _make_config(self, accounts: list[dict[str, Any]], models: list[str]) -> Config:
        from opencode_rate_limiter import DaemonConfig

        return Config(
            daemon=DaemonConfig(models=models),
            account_pool=AccountPoolConfig(accounts=accounts, strategy="round_robin"),
        )

    @pytest.mark.asyncio
    async def test_probe_carries_account_auth_token(self, httpx_mock, monkeypatch):
        """探测请求携带所选账号的 Authorization 头"""
        import argparse

        from opencode_rate_limiter import cmd_probe

        monkeypatch.setattr("opencode_rate_limiter.cli.get_opencode_version", lambda: "1.0")
        httpx_mock.add_response(url=ModelProber.ZEN_ENDPOINT, status_code=200, json={})
        config = self._make_config(
            accounts=[{"name": "primary", "auth_json": '{"access_token": "tok-1"}'}],
            models=["m1"],
        )

        rc = await cmd_probe(config, argparse.Namespace(model="all", json=True))
        assert rc == 0
        request = httpx_mock.get_requests()[0]
        assert request.headers["authorization"] == "Bearer tok-1"

    @pytest.mark.asyncio
    async def test_probe_rotates_tokens_across_models(self, httpx_mock, monkeypatch):
        """多模型探测按账号轮换，各自携带不同 token"""
        import argparse

        from opencode_rate_limiter import cmd_probe

        monkeypatch.setattr("opencode_rate_limiter.cli.get_opencode_version", lambda: "1.0")
        for _ in range(2):
            httpx_mock.add_response(url=ModelProber.ZEN_ENDPOINT, status_code=200, json={})
        config = self._make_config(
            accounts=[
                {"name": "a", "auth_json": '{"access_token": "tok-a"}'},
                {"name": "b", "auth_json": '{"access_token": "tok-b"}'},
            ],
            models=["m1", "m2"],
        )

        rc = await cmd_probe(config, argparse.Namespace(model="all", json=True))
        assert rc == 0
        auth_headers = {req.headers["authorization"] for req in httpx_mock.get_requests()}
        assert auth_headers == {"Bearer tok-a", "Bearer tok-b"}

    @pytest.mark.asyncio
    async def test_probe_without_resolvable_token_uses_plain_headers(self, httpx_mock, monkeypatch):
        """token 无法解析时退回无鉴权头（保持原行为）"""
        import argparse

        from opencode_rate_limiter import cmd_probe

        monkeypatch.setattr("opencode_rate_limiter.cli.get_opencode_version", lambda: "1.0")
        httpx_mock.add_response(url=ModelProber.ZEN_ENDPOINT, status_code=200, json={})
        config = self._make_config(accounts=[{"name": "a", "auth_json": "{}"}], models=["m1"])

        rc = await cmd_probe(config, argparse.Namespace(model="all", json=True))
        assert rc == 0
        request = httpx_mock.get_requests()[0]
        assert "authorization" not in request.headers


# =============================================================================
# Iteration round 2: [prober] config section + sliding-window health
# =============================================================================


class TestProberConfig:
    def test_defaults(self):
        cfg = ProberConfig()
        assert cfg.endpoint == ModelProber.ZEN_ENDPOINT
        assert cfg.ping_message == "ping"
        assert cfg.max_tokens == 1
        assert cfg.extra_headers == {}
        assert cfg.proxy is None

    def test_validate_rejects_bad_endpoint(self):
        with pytest.raises(ValueError, match="endpoint"):
            ProberConfig(endpoint="ftp://x").validate()

    def test_validate_rejects_zero_max_tokens(self):
        with pytest.raises(ValueError, match="max_tokens"):
            ProberConfig(max_tokens=0).validate()

    def test_validate_accepts_valid(self):
        ProberConfig(endpoint="http://localhost:9999/v1", max_tokens=2).validate()


class TestModelProberCustomConfig:
    @pytest.mark.asyncio
    async def test_custom_endpoint(self, httpx_mock):
        url = "https://example.com/zen/v1/chat/completions"
        httpx_mock.add_response(url=url, status_code=200, json={})
        prober = ModelProber(10.0, ProberConfig(endpoint=url))
        result = await prober.probe("m1", {})
        assert result.status == "available"

    @pytest.mark.asyncio
    async def test_extra_headers_sent(self, httpx_mock):
        httpx_mock.add_response(url=ModelProber.ZEN_ENDPOINT, status_code=200, json={})
        prober = ModelProber(10.0, ProberConfig(extra_headers={"X-Trace": "abc"}))
        await prober.probe("m1", {"User-Agent": "test"})
        request = httpx_mock.get_requests()[0]
        assert request.headers["x-trace"] == "abc"
        assert request.headers["user-agent"] == "test"  # shared headers preserved

    @pytest.mark.asyncio
    async def test_custom_payload(self, httpx_mock):
        httpx_mock.add_response(url=ModelProber.ZEN_ENDPOINT, status_code=200, json={})
        prober = ModelProber(10.0, ProberConfig(ping_message="hello", max_tokens=3))
        await prober.probe("m1", {})
        body = json.loads(httpx_mock.get_requests()[0].content)
        assert body["messages"][0]["content"] == "hello"
        assert body["max_tokens"] == 3


class TestHealthSlidingWindow:
    def test_window_reflects_recent_results(self):
        h = AccountHealth(name="a", window=10)
        for _ in range(8):
            h.results.append(True)
        for _ in range(2):
            h.results.append(False)
        assert h.success_rate == pytest.approx(0.8)

    def test_window_evicts_old_results(self):
        h = AccountHealth(name="a", window=5)
        for _ in range(50):
            h.results.append(False)
        for _ in range(5):
            h.results.append(True)
        assert h.success_rate == pytest.approx(1.0)
        assert len(h.results) == 5

    def test_mark_result_appends_to_window(self):
        config = AccountPoolConfig(accounts=[{"name": "a", "auth_json": "{}"}], health_window=3)
        pool = AccountPool(config)
        for success in (True, True, True, False):
            pool.mark_result("a", success=success)
        assert pool.health["a"].results.maxlen == 3
        assert list(pool.health["a"].results) == [True, True, False]

    def test_fallback_to_cumulative_when_window_empty(self):
        h = AccountHealth(name="a", success_count=7, total_count=10)
        assert h.success_rate == pytest.approx(0.7)

    def test_window_validation(self):
        with pytest.raises(ValueError, match="health_window"):
            AccountPoolConfig(accounts=[], health_window=0).validate()


# =============================================================================
# Iteration round 5: shared client / score weights
# =============================================================================


class TestProberSharedClient:
    @pytest.mark.asyncio
    async def test_probe_all_builds_one_client(self, httpx_mock, monkeypatch):
        """probe_all 全程只建一个共享 AsyncClient"""
        for _ in range(3):
            httpx_mock.add_response(url=ModelProber.ZEN_ENDPOINT, status_code=200, json={})
        prober = ModelProber(10.0)
        calls = []
        orig = prober._build_client

        def counting():
            calls.append(1)
            return orig()

        monkeypatch.setattr(prober, "_build_client", counting)
        results = await prober.probe_all(["a", "b", "c"], {})
        assert len(results) == 3
        assert len(calls) == 1
        assert prober._shared_client is None  # 用完即清，防泄漏

    @pytest.mark.asyncio
    async def test_probe_standalone_builds_own_client(self, httpx_mock):
        """单独 probe() 使用一次性客户端"""
        httpx_mock.add_response(url=ModelProber.ZEN_ENDPOINT, status_code=200, json={})
        prober = ModelProber(10.0)
        result = await prober.probe("m1", {})
        assert result.status == "available"
        assert prober._shared_client is None

    @pytest.mark.asyncio
    async def test_http2_falls_back_without_h2(self, httpx_mock, monkeypatch, caplog):
        """http2=True 但缺 h2 包时回退 HTTP/1.1 并告警"""
        import logging

        httpx_mock.add_response(url=ModelProber.ZEN_ENDPOINT, status_code=200, json={})
        monkeypatch.setattr("importlib.util.find_spec", lambda _name: None)
        prober = ModelProber(10.0, ProberConfig(http2=True))
        with caplog.at_level(logging.WARNING, logger="prober"):
            result = await prober.probe("m1", {})
        assert result.status == "available"
        assert any("h2" in r.message for r in caplog.records)

    def test_pool_size_validation(self):
        with pytest.raises(ValueError, match="connection_pool_size"):
            ProberConfig(connection_pool_size=0).validate()


class TestScoreWeights:
    def test_calculate_score_custom_weights(self):
        h = AccountHealth(name="a", success_count=5, total_count=10)
        # success 权重 1.0 时评分即成功率
        assert h.calculate_score({"success": 1.0, "latency": 0.0, "recency": 0.0}) == 0.5

    def test_weights_validation(self):
        with pytest.raises(ValueError, match="missing keys"):
            AccountPoolConfig(score_weights={"success": 1.0, "latency": 0.0}).validate()
        with pytest.raises(ValueError, match="unknown keys"):
            AccountPoolConfig(
                score_weights={"success": 0.5, "latency": 0.3, "recency": 0.1, "luck": 0.1}
            ).validate()
        with pytest.raises(ValueError, match=r"sum to 1\.0"):
            AccountPoolConfig(
                score_weights={"success": 0.5, "latency": 0.5, "recency": 0.5}
            ).validate()
        with pytest.raises(ValueError, match="within"):
            AccountPoolConfig(
                score_weights={"success": 2.0, "latency": -1.0, "recency": 0.0}
            ).validate()
        # 合法权重通过
        AccountPoolConfig(score_weights={"success": 0.8, "latency": 0.1, "recency": 0.1}).validate()

    def test_healthiest_respects_weights(self):
        config = AccountPoolConfig(
            accounts=[
                {"name": "hi-success", "auth_json": "{}"},
                {"name": "hi-latency", "auth_json": "{}"},
            ],
            strategy="health",
            score_weights={"success": 1.0, "latency": 0.0, "recency": 0.0},
        )
        pool = AccountPool(config)
        pool.health["hi-success"].success_count = 9
        pool.health["hi-success"].total_count = 10
        pool.health["hi-success"].avg_latency_ms = 2000
        pool.health["hi-latency"].success_count = 5
        pool.health["hi-latency"].total_count = 10
        pool.health["hi-latency"].avg_latency_ms = 100
        pick = pool.get_next()
        assert pick is not None and pick.name == "hi-success"

        pool.config.score_weights = {"success": 0.0, "latency": 1.0, "recency": 0.0}
        pick = pool.get_next()
        assert pick is not None and pick.name == "hi-latency"


class TestProbeAccountTag:
    """probe 输出标注账号"""

    def _make_config(self, accounts: list[dict[str, Any]], models: list[str]) -> Config:
        from opencode_rate_limiter import DaemonConfig

        return Config(
            daemon=DaemonConfig(models=models),
            account_pool=AccountPoolConfig(accounts=accounts, strategy="round_robin"),
        )

    @pytest.mark.asyncio
    async def test_probe_human_output_tags_account(self, httpx_mock, monkeypatch, capsys):
        """人读输出标注服务该模型的账号"""
        import argparse

        from opencode_rate_limiter import cmd_probe

        monkeypatch.setattr("opencode_rate_limiter.cli.get_opencode_version", lambda: "1.0")
        httpx_mock.add_response(url=ModelProber.ZEN_ENDPOINT, status_code=200, json={})
        config = self._make_config(
            accounts=[{"name": "primary", "auth_json": '{"access_token": "tok"}'}],
            models=["m1"],
        )

        rc = await cmd_probe(config, argparse.Namespace(model="all", json=False))
        assert rc == 0
        out = capsys.readouterr().out
        assert "[account: primary]" in out

    @pytest.mark.asyncio
    async def test_probe_json_includes_account(self, httpx_mock, monkeypatch, capsys):
        """JSON 输出包含 account 字段"""
        import argparse

        from opencode_rate_limiter import cmd_probe

        monkeypatch.setattr("opencode_rate_limiter.cli.get_opencode_version", lambda: "1.0")
        httpx_mock.add_response(url=ModelProber.ZEN_ENDPOINT, status_code=200, json={})
        config = self._make_config(
            accounts=[{"name": "primary", "auth_json": '{"access_token": "tok"}'}],
            models=["m1"],
        )

        rc = await cmd_probe(config, argparse.Namespace(model="all", json=True))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data[0]["account"] == "primary"
