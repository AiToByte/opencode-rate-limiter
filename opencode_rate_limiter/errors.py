"""Unified error classification for Zen gateway / opencode client failures.

All keyword knowledge lives here so `prober` (live HTTP), `diagnostics`
(human findings) and the `explain` command (offline log pastes) share one
source of truth. Matching is case-insensitive substring search; HTTP status
codes always take precedence over body text so an unknown message can never
downgrade a real 429 into a generic error (or vice versa).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ErrorKind = Literal[
    "rate_limited",
    "transient_transport",
    "reasoning_replay",
    "upstream",
    "auth",
    "server",
    "unknown",
]

# Substring markers (all lowercase). Kept intentionally narrow: a marker must
# be distinctive enough to survive being pasted inside a larger opencode log
# line without false-positiving on normal prose.
RATE_LIMIT_MARKERS: tuple[str, ...] = (
    "rate limit exceeded",
    "rate_limit",
    "ratelimit",
    "freeusagelimit",
    "too many requests",
    "quota exceeded",
    "quota exhausted",
    "daily limit",
    "limit exceeded",
)

TRANSIENT_MARKERS: tuple[str, ...] = (
    "socket connection was closed",
    "closed unexpectedly",
    "connection reset",
    "econnreset",
    "broken pipe",
    "connection was closed",
    "remote protocol",
    "socket closed",
    "connection closed",
    "temporarily unavailable",
    "transport error",
)

REASONING_MARKERS: tuple[str, ...] = (
    "encrypted_content",
    "not issued to this caller",
)

UPSTREAM_MARKERS: tuple[str, ...] = (
    "upstream request failed",
    "invalid_request_error",
    "invalid_encrypted_content",
)

AUTH_MARKERS: tuple[str, ...] = (
    "autherror",
    "auth failed",
    "unauthorized",
    "invalid api key",
    "invalid_api_key",
    "missing api key",
)

SERVER_MARKERS: tuple[str, ...] = (
    "server_error",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
)


@dataclass(frozen=True)
class ClassifyResult:
    """Outcome of classifying one error text / HTTP triple."""

    kind: ErrorKind
    matched: str | None = None
    normalized_type: str | None = None


def _contains_any(haystack: str, markers: tuple[str, ...]) -> str | None:
    for marker in markers:
        if marker in haystack:
            return marker
    return None


def _joined_blob(*parts: str | None) -> str:
    return " ".join(p for p in parts if p).lower()


def classify_http(
    http_status: int | None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> ClassifyResult:
    """Classify a live gateway response.

    Precedence: explicit 429 > reasoning replay > rate-limit text fallback >
    upstream > auth > server/5xx > transient text > unknown.
    """
    blob = _joined_blob(error_type, error_message)

    if http_status == 429:
        return ClassifyResult(
            kind="rate_limited",
            matched="http-429",
            normalized_type=error_type or "RateLimitUnknown",
        )

    hit = _contains_any(blob, REASONING_MARKERS)
    if hit is not None:
        return ClassifyResult(
            kind="reasoning_replay",
            matched=hit,
            normalized_type=error_type or "ReasoningReplayError",
        )

    hit = _contains_any(blob, RATE_LIMIT_MARKERS)
    if hit is not None:
        return ClassifyResult(
            kind="rate_limited",
            matched=hit,
            normalized_type=error_type or "RateLimitUnknown",
        )

    hit = _contains_any(blob, UPSTREAM_MARKERS)
    if hit is not None:
        return ClassifyResult(
            kind="upstream",
            matched=hit,
            normalized_type=error_type or "UpstreamError",
        )

    hit = _contains_any(blob, AUTH_MARKERS)
    if hit is not None:
        return ClassifyResult(kind="auth", matched=hit, normalized_type=error_type or "AuthError")

    if http_status is not None and 500 <= http_status <= 599:
        return ClassifyResult(
            kind="server",
            matched=f"http-{http_status}",
            normalized_type=error_type or "server_error",
        )
    hit = _contains_any(blob, SERVER_MARKERS)
    if hit is not None:
        return ClassifyResult(
            kind="server", matched=hit, normalized_type=error_type or "server_error"
        )

    hit = _contains_any(blob, TRANSIENT_MARKERS)
    if hit is not None:
        return ClassifyResult(
            kind="transient_transport",
            matched=hit,
            normalized_type=error_type or "TransientTransport",
        )

    return ClassifyResult(kind="unknown", normalized_type=error_type)


def classify_transport(message: str | None) -> ClassifyResult:
    """Classify an exception string / transport failure (no HTTP status)."""
    if not message:
        return ClassifyResult(kind="unknown")
    blob = message.lower()
    # A pasted opencode line can embed gateway text; reasoning wins so a
    # poisoned session is never misread as a mere network blip.
    hit = _contains_any(blob, REASONING_MARKERS)
    if hit is not None:
        return ClassifyResult(kind="reasoning_replay", matched=hit)
    hit = _contains_any(blob, RATE_LIMIT_MARKERS)
    if hit is not None:
        return ClassifyResult(kind="rate_limited", matched=hit)
    hit = _contains_any(blob, TRANSIENT_MARKERS)
    if hit is not None:
        return ClassifyResult(kind="transient_transport", matched=hit)
    hit = _contains_any(blob, UPSTREAM_MARKERS)
    if hit is not None:
        return ClassifyResult(kind="upstream", matched=hit)
    return ClassifyResult(kind="unknown")


def classify_opencode_log_line(line: str) -> ClassifyResult:
    """Classify one pasted opencode TUI / log line (offline, no network)."""
    if not line:
        return ClassifyResult(kind="unknown")
    blob = line.lower()
    table: tuple[tuple[tuple[str, ...], ErrorKind], ...] = (
        (REASONING_MARKERS, "reasoning_replay"),
        (RATE_LIMIT_MARKERS, "rate_limited"),
        (TRANSIENT_MARKERS, "transient_transport"),
        (UPSTREAM_MARKERS, "upstream"),
        (AUTH_MARKERS, "auth"),
        (SERVER_MARKERS, "server"),
    )
    for markers, kind in table:
        hit = _contains_any(blob, markers)
        if hit is not None:
            return ClassifyResult(kind=kind, matched=hit)
    return ClassifyResult(kind="unknown")


_EXPLAIN_COPY: dict[ErrorKind, tuple[str, str, str]] = {
    "rate_limited": (
        "服务端限额（免费配额/ key RPM）",
        "网关按出口 IP 统计免费日配额（UTC 午夜重置），按 key 统计付费 RPM。"
        "本地操作无法解除配额，只能等待或更换出口 IP。",
        "等待 UTC 午夜重置；换不同服务商/地区的出口 IP（IPv6 同 /64 无效）；"
        "暂停探测省配额，用 diagnose（1 次配额）或 explain（零配额）确认。",
    ),
    "transient_transport": (
        "传输层瞬时中断（非限额）",
        "opencode 与网关/代理之间的长连接被掐断（网关负载、代理不稳、"
        "context 过大导致 stream 超时都可能触发）。与配额无关，但限额前后高并发时更常见。",
        "重试一次；确认 HTTPS_PROXY/TUN 生效（curl https://api.ipify.org 对比）；"
        "大 context 先 /compact；仍失败则开新会话验证。",
    ),
    "reasoning_replay": (
        "推理加密块会话污染（非限额）",
        "Anthropic 系 reasoning.encrypted_content 绑定签发时的凭证/模型/区域，"
        "只能原样回放。中途换 key/换账号/换模型、--continue 老会话、"
        "网关上游换 key/跨区路由都会 400，且之后每轮都失败。",
        "当前会话执行 /clear 或开新会话（勿 --continue）；同会话内不换模型不换账号；"
        "切勿轮换账号抢救，会加重污染。",
    ),
    "upstream": (
        "网关/上游错误（非限额）",
        "请求已到达 Zen 网关，但上游 provider 返回错误，与本地网络和配额无关。",
        "稍后重试或更换模型；持续出现看 OpenCode 状态页。",
    ),
    "auth": (
        "鉴权失败",
        "凭证缺失/无效；免费模型本可匿名访问，先检查配置的凭证是否过期或写错。",
        "检查 auth.json / API key，必要时重新登录；免费模型可匿名对比验证。",
    ),
    "server": (
        "网关 5xx / 服务端错误",
        "网关自身故障或过载，与配额无关。",
        "稍后重试；持续则换模型或看状态页，不要高频重试烧配额。",
    ),
    "unknown": (
        "未知错误",
        "文本中没有可识别的限额/传输/上游特征，需要更多上下文（HTTP 状态码、"
        "error.type、verbose 日志）进一步判断。",
        "用 diagnose 跑一次单模型探测，或带 --verbose 复现后粘贴完整行再 explain。",
    ),
}


def explain_kind(kind: ErrorKind) -> tuple[str, str, str]:
    """Return (title, detail, remedy) copy for an ErrorKind (offline-safe)."""
    return _EXPLAIN_COPY[kind]
