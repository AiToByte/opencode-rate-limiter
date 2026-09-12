"""Phase 2 module tests - HeaderInjector, ModelProber, AccountPool, CleanupManager."""

import pytest

from opencode_rate_limiter import (
    AccountHealth,
    AccountPool,
    AccountPoolConfig,
    CleanupConfig,
    CleanupManager,
    CleanupResult,
    HeaderInjector,
    HeadersConfig,
    ModelProber,
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
        assert result.estimated_reset is not None
        assert 1 <= result.estimated_reset <= 86400

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
        """无 Retry-After 时估算为距 UTC 午夜的秒数（免费层真实重置点）"""
        import datetime as _dt

        reset = ModelProber._estimate_reset(None)
        now = _dt.datetime.now(_dt.UTC)
        midnight = (now + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        expected = max(1, int((midnight - now).total_seconds()))
        assert reset == expected
        assert 1 <= reset <= 86400


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
    def test_backup_auth_files(self, tmp_path):
        """备份 auth.json 且内容原样保留（不做 token 手术）"""
        auth_file = tmp_path / "auth.json"
        original = '{"https://opencode.ai/zen": {"type": "oauth", "access": "tok"}}'
        auth_file.write_text(original)
        mgr = CleanupManager(CleanupConfig())
        result = mgr.backup_auth_files([auth_file], dry_run=False)
        assert result.cleared_count == 1
        backup = auth_file.with_suffix(".json.bak")
        assert backup.exists()
        assert backup.read_text(encoding="utf-8") == original
        # 原文件未被修改
        assert auth_file.read_text(encoding="utf-8") == original

    def test_backup_auth_dry_run(self, tmp_path):
        auth_file = tmp_path / "auth.json"
        auth_file.write_text("{}")
        mgr = CleanupManager(CleanupConfig())
        result = mgr.backup_auth_files([auth_file], dry_run=True)
        assert result.cleared_count == 1
        assert not auth_file.with_suffix(".json.bak").exists()

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


class TestExtractAccessTokenRealAuth:
    """opencode 真实 auth.json 结构（源码核实）的 token 解析"""

    def test_oauth_entry_shape(self):
        """opencode OAuth 条目：{type: oauth, access, refresh, expires}"""
        auth = {"type": "oauth", "access": "tok-1", "refresh": "r", "expires": 1}
        assert extract_access_token(auth) == "tok-1"

    def test_provider_keyed_shape(self):
        """真实 auth.json 顶层按 provider 键存储"""
        auth = {
            "https://opencode.ai/zen": {
                "type": "oauth",
                "access": "tok-zen",
                "refresh": "r",
                "expires": 1,
            }
        }
        assert extract_access_token(auth) == "tok-zen"

    def test_generic_access_token_still_supported(self):
        assert extract_access_token({"access_token": "tok"}) == "tok"

    def test_no_token(self):
        assert extract_access_token({"type": "api", "key": "k"}) is None
