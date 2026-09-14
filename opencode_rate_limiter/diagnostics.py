"""Environment and rate-limit diagnostics for the Zen free tier.

`run_diagnostics()` produces a structured report that answers: is my traffic
actually leaving through the IP I think it is, which layer of the gateway's
rate limiting am I hitting, and what can (and cannot) be done about it.

Robustness rules:
- every external call has a timeout and degrades to "unknown" findings
- secrets (tokens / API keys) are never included in the report
- no unhandled exceptions escape: a diagnosis always returns a verdict
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import ipaddress
import json
import logging
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import Config
from .headers import HeaderInjector
from .paths import get_opencode_auth_files, get_opencode_version
from .pool import credential_fingerprint, extract_credential
from .prober import ModelProber, ProbeResult, seconds_to_utc_midnight

logger = logging.getLogger("diagnostics")

# Public IP echo services, tried in order; the answer must parse as an IP.
IP_ECHO_SERVICES = (
    "https://api.ipify.org",
    "https://icanhazip.com",
    "https://ifconfig.me/ip",
)
# Best-effort enrichment (org/country); failure is non-fatal.
IP_META_SERVICE = "https://ipinfo.io/json"

PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")

SEVERITY_WEIGHT = {"info": 0, "ok": 1, "warn": 2, "fail": 3}


@dataclass
class Finding:
    """One diagnostic observation, human-readable and actionable"""

    severity: str  # ok | info | warn | fail
    title: str
    detail: str = ""
    remedy: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "remedy": self.remedy,
        }


@dataclass
class Diagnosis:
    """Full diagnostic report"""

    timestamp: str = ""
    model: str = ""
    verdict: str = "unknown"  # ok | rate_limited | error
    exit_code: int = 0
    egress: dict[str, Any] = field(default_factory=dict)
    proxy_env: dict[str, str | None] = field(default_factory=dict)
    auth_files: list[dict[str, Any]] = field(default_factory=list)
    probe: dict[str, Any] | None = None
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "model": self.model,
            "verdict": self.verdict,
            "exit_code": self.exit_code,
            "egress": self.egress,
            "proxy_env": self.proxy_env,
            "auth_files": self.auth_files,
            "probe": self.probe,
            "findings": [f.to_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Network helpers (sync; the caller wraps them in asyncio.to_thread)
# ---------------------------------------------------------------------------


def fetch_public_ip(timeout: float = 5.0) -> str | None:
    """Best-effort public egress IP, validated with the ipaddress module.

    Uses the default httpx environment (i.e. honours HTTP(S)_PROXY), so the
    result reflects the same egress path our probes - and tools with the same
    proxy settings - would take.
    """
    for url in IP_ECHO_SERVICES:
        try:
            resp = httpx.get(url, timeout=timeout, follow_redirects=True)
            text = resp.text.strip()
            ipaddress.ip_address(text)  # must parse as an IP, else try next
            return text
        except Exception as e:
            logger.debug("IP echo %s failed: %s", url, e)
    return None


def fetch_ip_meta(timeout: float = 4.0) -> dict[str, Any]:
    """Best-effort org/country enrichment; returns {} on any failure."""
    try:
        resp = httpx.get(IP_META_SERVICE, timeout=timeout)
        data = resp.json()
        if not isinstance(data, dict):
            return {}
        return {
            "country": data.get("country"),
            "org": data.get("org"),
            "city": data.get("city"),
        }
    except Exception as e:
        logger.debug("IP metadata lookup failed: %s", e)
        return {}


def collect_proxy_env() -> dict[str, str | None]:
    """Collect proxy-related environment variables (both cases)."""
    result: dict[str, str | None] = {}
    for name in PROXY_ENV_VARS:
        value = os.environ.get(name) or os.environ.get(name.lower())
        result[name] = value
    return result


def ipv6_prefix(ip: str) -> str | None:
    """For IPv6 addresses, the /64 prefix the gateway aggregates by"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.version != 6:
        return None
    network = ipaddress.ip_network(f"{addr}/64", strict=False)
    return f"{network.network_address}/64"


def _auth_shape(data: Any) -> tuple[str, bool]:
    """Classify an auth.json payload without exposing any secret values"""
    if not isinstance(data, dict) or not data:
        return "empty", False
    if data.get("type") in ("oauth", "api", "wellknown"):
        has = extract_credential(data) is not None
        return f"single-entry:{data.get('type')}", has
    shapes: set[str] = set()
    has_token = False
    for value in data.values():
        if isinstance(value, dict):
            shapes.add(str(value.get("type", "?")))
            if extract_credential(value) is not None:
                has_token = True
    if shapes:
        return f"provider-keyed:{','.join(sorted(shapes))}", has_token
    return "unknown", False


def _credential_summaries(data: Any) -> list[dict[str, str]]:
    """Fingerprint credentials found in an auth payload (never the values)"""
    summaries: list[dict[str, str]] = []
    if isinstance(data, dict):
        top = extract_credential(data)
        if top:
            summaries.append({"kind": top[0], "fingerprint": credential_fingerprint(top[1])})
            return summaries
        for value in data.values():
            if isinstance(value, dict):
                cred = extract_credential(value)
                if cred:
                    summaries.append(
                        {"kind": cred[0], "fingerprint": credential_fingerprint(cred[1])}
                    )
    return summaries


def inspect_auth_files() -> list[dict[str, Any]]:
    """Inventory opencode auth.json candidates (existence/shape, no secrets)"""
    entries: list[dict[str, Any]] = []
    for path in get_opencode_auth_files():
        entry: dict[str, Any] = {
            "path": str(path),
            "exists": path.exists(),
            "shape": None,
            "has_token": False,
            "credentials": [],
        }
        if path.exists():
            try:
                if path.stat().st_size > 1_000_000:
                    entry["shape"] = "unreadable (too large)"
                else:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    entry["shape"], entry["has_token"] = _auth_shape(data)
                    entry["credentials"] = _credential_summaries(data)
            except Exception as e:
                entry["shape"] = f"unreadable ({type(e).__name__})"
        entries.append(entry)
    return entries


def endpoint_bypassed_by_no_proxy(endpoint: str, no_proxy: str | None) -> bool:
    """Whether NO_PROXY matches the endpoint host (the request goes direct).

    Pure helper around `urllib.request.proxy_bypass_environment` (whose
    availability/behaviour varies by platform) so it stays unit-testable.
    """
    if not no_proxy:
        return False
    try:
        host = urlsplit(endpoint).hostname or ""
        # dynamic lookup: typeshed gates proxy_bypass_environment by platform
        bypass_fn = getattr(urllib.request, "proxy_bypass_environment")  # noqa: B009
        return bool(host) and bool(bypass_fn(host, {"no": no_proxy}))
    except Exception:
        return False


def _reset_time_strings() -> tuple[int, str]:
    """Seconds to UTC midnight + a human string with UTC and local clock"""
    seconds = seconds_to_utc_midnight()
    now_utc = _dt.datetime.now(_dt.UTC)
    midnight = (now_utc + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    local_tz = _dt.datetime.now().astimezone().tzinfo
    local_midnight = midnight.astimezone(local_tz)
    human = f"{midnight:%Y-%m-%d %H:%M} UTC（本地 {local_midnight:%m-%d %H:%M}）"
    return seconds, human


# ---------------------------------------------------------------------------
# Findings assembly
# ---------------------------------------------------------------------------

_ERROR_TYPE_EXPLANATIONS: dict[str, tuple[str, str]] = {
    # error.type -> (title, what it means; remedies added per-case)
    "FreeUsageLimitError": (
        "免费层每日配额用尽（按 IP 计数）",
        "网关按出口 IP 统计每日请求数（UTC 午夜重置）。配额键是 IP 而非账号，"
        "换账号不会增加免费额度。",
    ),
    "RateLimitError": (
        "付费 key 超过每分钟限额（默认 1000 RPM/key/模型）",
        "该限制按分钟窗口计数，等待约 1 分钟即可恢复。",
    ),
    "BlackUsageLimitError": (
        "黑金/订阅层限额触发",
        "属于账号层限制，与出口 IP 无关。",
    ),
    "MonthlyLimitError": ("月度额度用尽", "属于账号计费层限制，与出口 IP 无关。"),
    "UserLimitError": ("账号级限额触发", "属于账号层限制，与出口 IP 无关。"),
    "CreditsError": ("余额不足", "需要在 OpenCode 控制台充值或改用免费模型。"),
    "AuthError": ("鉴权失败", "API key 缺失或无效；免费模型本可匿名访问，请检查凭证配置。"),
    "RegionError": ("地区限制", "当前出口 IP 所在地区被该模型限制，更换节点地区可能解决。"),
    "GoUsageLimitError": ("Go 订阅窗口限额（5 小时/周/月）", "属于订阅计费层限制。"),
    "server_error": (
        "网关/上游错误",
        "Zen 网关或其上游 provider 返回错误（与配额无关）。稍后重试、更换模型，"
        "或查看 OpenCode 状态页。",
    ),
}


def _probe_findings(result: ProbeResult, model: str) -> tuple[list[Finding], str, int]:
    """Build findings from a single probe; return (findings, verdict, exit_code)"""
    findings: list[Finding] = []
    if result.status == "available":
        findings.append(
            Finding(
                "ok",
                f"模型 {model} 当前可用",
                f"探测返回 200（延迟 {result.latency_ms:.0f}ms）。当前出口 IP 的免费配额尚未耗尽。",
            )
        )
        return findings, "ok", 0

    if result.status == "rate_limited":
        error_type = result.error_type or "unknown"
        expl = _ERROR_TYPE_EXPLANATIONS.get(error_type)
        title = expl[0] if expl else f"被限流（{error_type}）"
        detail = expl[1] if expl else "网关返回 429，具体层级未知。"
        findings.append(
            Finding("fail", title, detail + f"\n模型: {model}，error.type: {error_type}")
        )
        if error_type == "FreeUsageLimitError":
            seconds, human = _reset_time_strings()
            wait = result.retry_after if result.retry_after else seconds
            findings.append(
                Finding(
                    "info",
                    "配额重置时间",
                    f"retry-after = {wait}s，即 UTC 午夜重置：{human}。",
                )
            )
            findings.append(
                Finding(
                    "warn",
                    "换账号对此无效",
                    "免费配额键是出口 IP（Redis 按 IP 计数），不是账号。"
                    "同 IP 下切换 opencode 账号不会增加额度。",
                )
            )
            findings.append(
                Finding(
                    "warn",
                    "确认出口 IP 与节点切换是否生效",
                    "若你在用 Clash/V2Ray 等代理：终端 CLI 通常不读系统代理，"
                    "需要设置 HTTPS_PROXY 环境变量或开启 TUN 模式；"
                    "验证：curl https://api.ipify.org 对比开关代理后的输出。"
                    "共享机场节点可能已被其他用户耗尽当日配额；"
                    "IPv6 节点按 /64 前缀聚合，同网段换地址无效。",
                    remedy="换不同地区/不同服务商的出口 IP，或等待重置。",
                )
            )
        else:
            findings.append(
                Finding(
                    "info",
                    "该限制与出口 IP 无关",
                    "账号/key 维度的限额换 IP 或换账号（同凭证）都不会更快恢复。",
                    remedy="等待窗口重置或调整付费计划。",
                )
            )
        return findings, "rate_limited", 1

    # status == "error"
    if result.error_type is not None:
        # The body parsed as a Zen error: the request DID reach the gateway;
        # the failure is upstream/server-side, not our network path.
        expl = _ERROR_TYPE_EXPLANATIONS.get(result.error_type)
        title = expl[0] if expl else f"网关返回错误（{result.error_type}）"
        detail = expl[1] if expl else "Zen 网关返回了错误响应，与本地网络无关。"
        findings.append(
            Finding(
                "fail",
                title,
                f"HTTP {result.http_status}，error.type: {result.error_type}\n{detail}",
                remedy="稍后重试或更换模型；若持续出现，检查 OpenCode 状态页。",
            )
        )
        return findings, "error", 2

    findings.append(
        Finding(
            "fail",
            "探测请求失败（未到达网关限流层）",
            f"错误: {result.error or 'unknown'}。响应不是 Zen 错误格式，通常是网络/代理链路问题。",
        )
    )
    proxy = collect_proxy_env()
    if any(proxy.values()):
        findings.append(
            Finding(
                "warn",
                "检测到代理环境变量",
                f"{', '.join(k for k, v in proxy.items() if v)} 已设置。"
                "代理不可达或节点断连会导致探测直接失败。",
                remedy="确认本地代理端口（如 7897）正在监听且节点可用。",
            )
        )
    else:
        findings.append(
            Finding(
                "info",
                "未检测到代理环境变量",
                "若你依赖 Clash 等代理访问 opencode.ai：终端 CLI 不读系统代理，"
                "需要设置 HTTPS_PROXY 或开启 TUN 模式，否则流量直连。",
            )
        )
    return findings, "error", 2


async def run_diagnostics(config: Config, model: str | None = None) -> Diagnosis:
    """Run the full diagnostic suite and return a structured report"""
    loop = asyncio.get_running_loop()
    diagnosis = Diagnosis(timestamp=_now_iso())

    model_name = model or (config.daemon.models[0] if config.daemon.models else "unknown")
    diagnosis.model = model_name

    # 1. Environment: opencode version + proxy env (local, cannot fail)
    version = get_opencode_version()
    diagnosis.proxy_env = collect_proxy_env()
    diagnosis.findings.append(
        Finding(
            "ok" if version != "unknown" else "warn",
            f"opencode CLI 版本: {version}",
            "" if version != "unknown" else "未检测到 opencode CLI，请求头中的版本号为 unknown。",
        )
    )
    if any(diagnosis.proxy_env.values()):
        set_vars = {k: v for k, v in diagnosis.proxy_env.items() if v}
        diagnosis.findings.append(
            Finding(
                "info",
                "代理环境变量已设置",
                "; ".join(f"{k}={v}" for k, v in set_vars.items())
                + "\n本工具的探测与出口 IP 检测会经由该代理（httpx 遵循这些变量）；"
                "opencode CLI 是否同样走代理取决于其启动环境。",
            )
        )
        no_proxy = diagnosis.proxy_env.get("NO_PROXY")
        if no_proxy and endpoint_bypassed_by_no_proxy(config.prober.endpoint, no_proxy):
            diagnosis.findings.append(
                Finding(
                    "warn",
                    "NO_PROXY 覆盖了探测端点",
                    f"NO_PROXY={no_proxy} 匹配 {config.prober.endpoint} ——"
                    "该请求将绕过代理直连，出口 IP 为本机网络而非代理节点。",
                    remedy="从 NO_PROXY 中移除 opencode.ai / 相关条目。",
                )
            )

    # 2. Egress IP (best effort, never fatal)
    ip = await loop.run_in_executor(None, fetch_public_ip)
    egress: dict[str, Any] = {"ip": ip}
    if ip:
        egress["version"] = ipaddress.ip_address(ip).version
        prefix = ipv6_prefix(ip)
        if prefix:
            egress["ipv6_prefix"] = prefix
            diagnosis.findings.append(
                Finding(
                    "warn",
                    f"出口为 IPv6: {ip}",
                    f"网关按 /64 前缀聚合（当前前缀 {prefix}），"
                    "同网段内更换 IPv6 地址不会更换配额桶。",
                )
            )
        meta = await loop.run_in_executor(None, fetch_ip_meta)
        egress["meta"] = meta
        org = meta.get("org")
        diagnosis.findings.append(
            Finding(
                "ok",
                f"当前出口 IP: {ip}" + (f"（{org}）" if org else ""),
                "这是探测请求实际使用的出口。若与你预期的代理节点不符，"
                "说明代理对流量未生效（参见代理相关条目）。",
            )
        )
    else:
        egress["version"] = None
        diagnosis.findings.append(
            Finding(
                "warn",
                "无法获取出口 IP",
                "所有 IP 回显服务均失败（网络不通或被代理拦截）。"
                "可手动验证: curl https://api.ipify.org",
            )
        )
    diagnosis.egress = egress

    # 3. Auth inventory (local, no secrets)
    diagnosis.auth_files = inspect_auth_files()
    live = [a for a in diagnosis.auth_files if a["exists"]]
    if live:
        for a in live:
            diagnosis.findings.append(
                Finding(
                    "info",
                    f"auth 文件: {a['path']}",
                    f"结构: {a['shape']}，含可用凭证: {'是' if a['has_token'] else '否'}。",
                )
            )
        diagnosis.findings.append(
            Finding(
                "info",
                "免费模型不需要凭证",
                "Zen 免费模型允许匿名访问，且配额按 IP 而非账号统计——"
                "auth.json 里是什么账号不影响免费额度。",
            )
        )
    else:
        diagnosis.findings.append(
            Finding(
                "ok",
                "未找到 opencode auth.json",
                "免费模型匿名可用；凭证仅在使用付费 Zen key / BYOK 时需要。",
            )
        )

    # 4. Single probe (1 request — 计入每日配额，因此只探一个模型).
    # When an account pool is configured, use the same per-account injection
    # as `probe`/daemon so key-dimension limits are not misread as IP limits.
    version_for_headers = version if version != "unknown" else "unknown"
    injector = HeaderInjector(config.headers, version_for_headers)
    probe_headers = injector.build_headers()
    diagnosis_account: str | None = None
    if config.account_pool.accounts:
        from .pool import AccountPool

        pool = AccountPool(config.account_pool)
        account = pool.get_next()
        if account is not None:
            diagnosis_account = account.name
            token = pool.resolve_token(account)
            if token:
                probe_headers = injector.build_headers(token=token)
    prober = ModelProber(config.daemon.probe_timeout_seconds, config.prober)
    try:
        result = await prober.probe(model_name, probe_headers)
    except Exception as e:  # defensive: prober should not raise, but never crash
        logger.debug("probe crashed: %s", e)
        result = ProbeResult(model=model_name, status="error", error=f"internal: {e}")
    diagnosis.probe = result.to_dict()
    if diagnosis_account:
        diagnosis.probe["account"] = diagnosis_account
        diagnosis.findings.append(
            Finding(
                "info",
                f"诊断使用账号: {diagnosis_account}",
                "探测携带该账号的凭证，与 probe/daemon 的轮换逻辑一致；"
                "匿名对比可用 `probe` 不配账号池时复现。",
            )
        )

    findings, verdict, exit_code = _probe_findings(result, model_name)
    diagnosis.findings.extend(findings)
    diagnosis.verdict = verdict
    diagnosis.exit_code = exit_code
    return diagnosis


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Human-readable rendering
# ---------------------------------------------------------------------------

_ICONS = {"ok": "OK  ", "info": "NOTE", "warn": "WARN", "fail": "FAIL"}


def format_report(diagnosis: Diagnosis) -> str:
    """Render the report as an indented, sectioned human summary"""
    lines: list[str] = [
        "Zen Free-Tier Diagnostics",
        "=" * 60,
        f"  时间: {diagnosis.timestamp}    模型: {diagnosis.model}",
        "",
        "-- 出口与网络 --",
    ]
    ip = diagnosis.egress.get("ip")
    if ip:
        lines.append(f"  出口 IP: {ip}（IPv{diagnosis.egress.get('version', '?')}）")
        if diagnosis.egress.get("ipv6_prefix"):
            lines.append(f"  IPv6 /64 前缀: {diagnosis.egress['ipv6_prefix']}")
        meta = diagnosis.egress.get("meta") or {}
        if meta.get("org"):
            lines.append(f"  归属: {meta.get('org', '')} {meta.get('country', '')}".rstrip())
    else:
        lines.append("  出口 IP: 未知（回显服务不可达）")
    proxy_lines = [f"{k}={v}" for k, v in diagnosis.proxy_env.items() if v]
    lines.append("  代理环境: " + ("; ".join(proxy_lines) if proxy_lines else "未设置"))

    lines.append("")
    lines.append("-- 凭证 --")
    live = [a for a in diagnosis.auth_files if a["exists"]]
    if live:
        for a in live:
            lines.append(f"  {a['path']}  [{a['shape']}, 凭证: {'有' if a['has_token'] else '无'}]")
    else:
        lines.append("  （未找到 auth.json——免费模型匿名可用）")

    probe = diagnosis.probe or {}
    lines.append("")
    lines.append("-- 探测（1 次请求，计入每日配额）--")
    lines.append(
        f"  模型 {probe.get('model', diagnosis.model)}: {probe.get('status', 'unknown')}"
        f" (HTTP {probe.get('http_status')}, {probe.get('latency_ms', 0):.0f}ms)"
    )
    if probe.get("error_type"):
        lines.append(f"  error.type: {probe['error_type']}")
    if probe.get("error"):
        lines.append(f"  错误信息: {probe['error']}")
    if probe.get("retry_after"):
        lines.append(f"  retry-after: {probe['retry_after']}s")

    lines.append("")
    lines.append("-- 发现 --")
    for f in diagnosis.findings:
        lines.append(f"  [{_ICONS.get(f.severity, '    ')}] {f.title}")
        for detail_line in (f.detail or "").splitlines():
            lines.append(f"         {detail_line}")
        if f.remedy:
            lines.append(f"         → 建议: {f.remedy}")

    verdict_text = {"ok": "健康", "rate_limited": "被限流", "error": "网络/其他错误"}.get(
        diagnosis.verdict, diagnosis.verdict
    )
    lines.append("")
    lines.append(f"-- 结论: {verdict_text} --")
    return "\n".join(lines)
