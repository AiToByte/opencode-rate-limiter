"""Diagnostics module tests: robustness, fallbacks, verdict mapping, redaction."""

import json
from typing import Any

import httpx
import pytest

from opencode_rate_limiter import (
    Config,
    Diagnosis,
    ProberConfig,
    ProbeResult,
    run_diagnostics,
)
from opencode_rate_limiter.diagnostics import (
    IP_ECHO_SERVICES,
    _auth_shape,
    collect_proxy_env,
    fetch_public_ip,
    format_report,
    inspect_auth_files,
    ipv6_prefix,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _patch_env(monkeypatch, tmp_path, probe_result: ProbeResult, ip: str | None = "203.0.113.7"):
    """Isolate a diagnose run: no real network, no real auth files, canned probe."""

    class FakeProber:
        def __init__(self, timeout: float = 10.0, config: ProberConfig | None = None):
            self.config = config or ProberConfig()

        async def probe(self, model: str, headers: dict[str, str]) -> ProbeResult:
            return probe_result

    monkeypatch.setattr("opencode_rate_limiter.diagnostics.ModelProber", FakeProber)
    monkeypatch.setattr("opencode_rate_limiter.diagnostics.get_opencode_version", lambda: "1.18.30")
    monkeypatch.setattr("opencode_rate_limiter.diagnostics.get_opencode_auth_files", lambda: [])
    if ip is None:
        monkeypatch.setattr(
            "opencode_rate_limiter.diagnostics.fetch_public_ip", lambda _timeout=5.0: None
        )
    else:
        monkeypatch.setattr(
            "opencode_rate_limiter.diagnostics.fetch_public_ip", lambda _timeout=5.0: ip
        )
    monkeypatch.setattr(
        "opencode_rate_limiter.diagnostics.fetch_ip_meta",
        lambda _timeout=4.0: {"country": "US", "org": "Example Hosting"},
    )
    # 隔离真实代理环境
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(var.lower(), raising=False)
    _ = tmp_path


def _result(**kwargs: Any) -> ProbeResult:
    base: dict[str, Any] = {
        "model": "m1",
        "status": "available",
        "http_status": 200,
        "latency_ms": 50.0,
        "timestamp": "t",
    }
    base.update(kwargs)
    return ProbeResult(**base)


# ---------------------------------------------------------------------------
# network helpers
# ---------------------------------------------------------------------------


class TestFetchPublicIp:
    def test_first_service_valid(self, httpx_mock):
        httpx_mock.add_response(url=IP_ECHO_SERVICES[0], text="203.0.113.7")
        assert fetch_public_ip() == "203.0.113.7"

    def test_falls_back_when_first_invalid(self, httpx_mock):
        """第一个服务返回非 IP 内容（如 HTML 错误页）时回退到下一个"""
        httpx_mock.add_response(url=IP_ECHO_SERVICES[0], status_code=502, text="<html>err</html>")
        httpx_mock.add_response(url=IP_ECHO_SERVICES[1], text="198.51.100.9\n")
        assert fetch_public_ip() == "198.51.100.9"

    def test_returns_none_when_all_fail(self, httpx_mock):
        for url in IP_ECHO_SERVICES:
            httpx_mock.add_response(url=url, status_code=500)
        assert fetch_public_ip() is None


class TestIpv6Prefix:
    def test_ipv4_returns_none(self):
        assert ipv6_prefix("203.0.113.7") is None

    def test_ipv6_prefix_aggregation(self):
        prefix = ipv6_prefix("2001:db8:1234:5678::1")
        assert prefix == "2001:db8:1234:5678::/64"

    def test_invalid_returns_none(self):
        assert ipv6_prefix("not-an-ip") is None


class TestCollectProxyEnv:
    def test_reads_upper_and_lower(self, monkeypatch):
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.setenv("https_proxy", "http://127.0.0.1:7897")
        env = collect_proxy_env()
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:7897"

    def test_missing_is_none(self, monkeypatch):
        monkeypatch.delenv("ALL_PROXY", raising=False)
        monkeypatch.delenv("all_proxy", raising=False)
        assert collect_proxy_env()["ALL_PROXY"] is None


# ---------------------------------------------------------------------------
# auth inventory (no secrets)
# ---------------------------------------------------------------------------


class TestAuthInventory:
    def test_provider_keyed_shape(self, tmp_path, monkeypatch):
        auth = tmp_path / "auth.json"
        auth.write_text(
            json.dumps({"https://opencode.ai/zen": {"type": "oauth", "access": "secret-token"}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "opencode_rate_limiter.diagnostics.get_opencode_auth_files", lambda: [auth]
        )
        entries = inspect_auth_files()
        assert entries[0]["shape"] == "provider-keyed:oauth"
        assert entries[0]["has_token"] is True
        # 凭证值绝不入盘点
        assert "secret-token" not in json.dumps(entries)

    def test_corrupt_file_reported_not_raised(self, tmp_path, monkeypatch):
        auth = tmp_path / "auth.json"
        auth.write_text("not json {", encoding="utf-8")
        monkeypatch.setattr(
            "opencode_rate_limiter.diagnostics.get_opencode_auth_files", lambda: [auth]
        )
        entries = inspect_auth_files()
        assert "unreadable" in entries[0]["shape"]

    def test_api_shape(self):
        shape, has = _auth_shape({"type": "api", "key": "k"})
        assert shape == "single-entry:api"
        assert has is True


# ---------------------------------------------------------------------------
# full diagnosis verdicts
# ---------------------------------------------------------------------------


class TestRunDiagnostics:
    @pytest.mark.asyncio
    async def test_verdict_ok_when_available(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, tmp_path, _result())
        diagnosis = await run_diagnostics(Config(), model="m1")
        assert diagnosis.verdict == "ok"
        assert diagnosis.exit_code == 0

    @pytest.mark.asyncio
    async def test_free_quota_exhausted_mapping(self, monkeypatch, tmp_path):
        probe = _result(
            status="rate_limited",
            http_status=429,
            retry_after=45000,
            estimated_reset=45000,
            error_type="FreeUsageLimitError",
        )
        _patch_env(monkeypatch, tmp_path, probe)
        diagnosis = await run_diagnostics(Config(), model="m1")

        assert diagnosis.verdict == "rate_limited"
        assert diagnosis.exit_code == 1
        titles = [f.title for f in diagnosis.findings]
        assert any("换账号对此无效" in t for t in titles)
        assert any("配额重置时间" in t for t in titles)
        assert any("确认出口 IP" in t for t in titles)

    @pytest.mark.asyncio
    async def test_gateway_error_classification(self, monkeypatch, tmp_path):
        """HTTP 400 + error.type=server_error 应归为网关错误而非网络问题"""
        probe = _result(
            status="error", http_status=400, error="HTTP 400", error_type="server_error"
        )
        _patch_env(monkeypatch, tmp_path, probe)
        diagnosis = await run_diagnostics(Config(), model="m1")

        assert diagnosis.exit_code == 2
        titles = [f.title for f in diagnosis.findings]
        assert any("网关/上游错误" in t for t in titles)
        assert not any("未到达网关限流层" in t for t in titles)

    @pytest.mark.asyncio
    async def test_timeout_maps_to_network(self, httpx_mock, monkeypatch, tmp_path):
        import httpx as _httpx

        httpx_mock.add_exception(_httpx.ConnectError("refused"))
        config = Config()  # 真实 prober，网络被 mock 拒绝
        monkeypatch.setattr("opencode_rate_limiter.diagnostics.get_opencode_version", lambda: "1.0")
        monkeypatch.setattr("opencode_rate_limiter.diagnostics.get_opencode_auth_files", lambda: [])
        monkeypatch.setattr(
            "opencode_rate_limiter.diagnostics.fetch_public_ip", lambda _timeout=5.0: None
        )
        diagnosis = await run_diagnostics(config, model="m1")
        assert diagnosis.verdict == "error"
        assert diagnosis.exit_code == 2
        assert any("未到达网关限流层" in f.title for f in diagnosis.findings)

    @pytest.mark.asyncio
    async def test_proxy_env_reported(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, tmp_path, _result())
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
        diagnosis = await run_diagnostics(Config(), model="m1")
        assert diagnosis.proxy_env["HTTPS_PROXY"] == "http://127.0.0.1:7897"
        assert any("代理环境变量已设置" in f.title for f in diagnosis.findings)

    @pytest.mark.asyncio
    async def test_report_contains_no_secrets(self, monkeypatch, tmp_path):
        probe = _result()
        _patch_env(monkeypatch, tmp_path, probe)
        config = Config()
        config.account_pool.accounts = [{"name": "a", "auth_json": '{"access": "SUPER-SECRET"}'}]

        diagnosis = await run_diagnostics(config, model="m1")
        rendered = format_report(diagnosis) + json.dumps(diagnosis.to_dict())
        assert "SUPER-SECRET" not in rendered

    @pytest.mark.asyncio
    async def test_human_report_sections(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, tmp_path, _result(status="rate_limited", http_status=429))
        diagnosis = await run_diagnostics(Config(), model="m1")
        report = format_report(diagnosis)
        for section in ("出口与网络", "凭证", "探测", "发现", "结论: 被限流"):
            assert section in report

    @pytest.mark.asyncio
    async def test_rate_limit_unknown_message_gets_reset_guidance(self, monkeypatch, tmp_path):
        probe = _result(
            status="rate_limited",
            http_status=400,
            error="Rate limit exceeded. Please try again later.",
            error_type="RateLimitUnknown",
            error_kind="rate_limited",
        )
        _patch_env(monkeypatch, tmp_path, probe)
        diagnosis = await run_diagnostics(Config(), model="m1")
        assert diagnosis.verdict == "rate_limited"
        titles = [f.title for f in diagnosis.findings]
        assert any("层级未知" in t or "被限流" in t for t in titles)
        assert any("重置" in t for t in titles)

    @pytest.mark.asyncio
    async def test_reasoning_replay_warns_no_rotation(self, monkeypatch, tmp_path):
        probe = _result(
            status="error",
            http_status=400,
            error="Upstream request failed: reasoning `encrypted_content` was not issued",
            error_type="invalid_request_error",
            error_kind="reasoning_replay",
        )
        _patch_env(monkeypatch, tmp_path, probe)
        diagnosis = await run_diagnostics(Config(), model="m1")
        assert diagnosis.verdict == "error"
        titles = [f.title for f in diagnosis.findings]
        assert any("会话污染" in t for t in titles)
        assert any("切勿轮换" in t for t in titles)

    @pytest.mark.asyncio
    async def test_transient_transport_maps_to_network(self, monkeypatch, tmp_path):
        probe = _result(
            status="error",
            http_status=None,
            error="Cannot connect to API: The socket connection was closed unexpectedly.",
            error_kind="transient_transport",
        )
        _patch_env(monkeypatch, tmp_path, probe)
        diagnosis = await run_diagnostics(Config(), model="m1")
        assert diagnosis.verdict == "error"
        assert any("未到达网关限流层" in f.title for f in diagnosis.findings)

    def test_diagnosis_defaults(self):
        d = Diagnosis()
        assert d.verdict == "unknown"
        assert d.to_dict()["findings"] == []


class TestProbeFindingsBranches:
    """Direct _probe_findings coverage: upstream / generic fallback / proxy hints."""

    def test_upstream_finding(self):
        from opencode_rate_limiter.diagnostics import _probe_findings

        findings, verdict, code = _probe_findings(
            _result(
                status="error",
                http_status=502,
                error="Upstream request failed: timeout",
                error_type="UpstreamError",
                error_kind="upstream",
            ),
            "m1",
        )
        assert verdict == "error" and code == 2
        assert any("上游" in f.title for f in findings)

    def test_generic_fallback_without_kind_or_type(self, monkeypatch):
        from opencode_rate_limiter.diagnostics import _probe_findings

        for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            monkeypatch.delenv(var, raising=False)
            monkeypatch.delenv(var.lower(), raising=False)
        findings, verdict, code = _probe_findings(
            _result(status="error", http_status=None, error="weird failure"), "m1"
        )
        assert verdict == "error" and code == 2
        assert any("未到达网关限流层" in f.title for f in findings)
        assert any("未检测到代理" in f.title for f in findings)

    def test_proxy_hint_when_proxy_set(self, monkeypatch):
        from opencode_rate_limiter.diagnostics import _probe_findings

        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
        findings, _, _ = _probe_findings(
            _result(
                status="error",
                http_status=None,
                error="socket connection was closed",
                error_kind="transient_transport",
            ),
            "m1",
        )
        assert any("检测到代理环境变量" in f.title for f in findings)


# httpx import guard (used by timeout test)
_ = httpx


class TestNoProxyMatching:
    """R4.5: NO_PROXY 精确判定"""

    @pytest.mark.asyncio
    async def test_no_proxy_covering_endpoint_warns_with_remedy(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, tmp_path, _result())
        monkeypatch.setenv("NO_PROXY", "opencode.ai,localhost")
        diagnosis = await run_diagnostics(Config(), model="m1")
        finding = next(f for f in diagnosis.findings if "NO_PROXY" in f.title)
        assert "绕过代理直连" in finding.detail
        assert finding.remedy is not None and "NO_PROXY" in finding.remedy

    @pytest.mark.asyncio
    async def test_unrelated_no_proxy_keeps_quiet(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, tmp_path, _result())
        monkeypatch.setenv("NO_PROXY", "localhost,example.com")
        diagnosis = await run_diagnostics(Config(), model="m1")
        assert not any("NO_PROXY" in f.title for f in diagnosis.findings)

    @pytest.mark.asyncio
    async def test_suffix_no_proxy_matches(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, tmp_path, _result())
        monkeypatch.setenv("NO_PROXY", ".ai,localhost")
        diagnosis = await run_diagnostics(Config(), model="m1")
        assert any("NO_PROXY 覆盖了探测端点" in f.title for f in diagnosis.findings)
