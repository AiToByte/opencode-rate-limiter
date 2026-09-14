"""Async availability probing of the Zen free-model endpoint."""

from __future__ import annotations

import datetime as _dt
import importlib.util
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from .config import ProberConfig


@dataclass
class ProbeResult:
    """Result of a model availability probe"""

    model: str
    status: str  # "available" | "rate_limited" | "error" | "unknown"
    http_status: int | None = None
    retry_after: int | None = None
    estimated_reset: int | None = None
    latency_ms: float = 0.0
    error: str | None = None
    error_type: str | None = None
    # Per-request token cost reported by the gateway (OpenAI-compatible
    # `usage` object). Informational only: the gateway exposes no quota
    # counters, so this cannot be turned into a "remaining" figure.
    usage: dict[str, int] | None = None
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "status": self.status,
            "http_status": self.http_status,
            "retry_after": self.retry_after,
            "estimated_reset": self.estimated_reset,
            "latency_ms": round(self.latency_ms, 1),
            "error": self.error,
            "error_type": self.error_type,
            "usage": self.usage,
            "timestamp": self.timestamp,
        }


class ModelProber:
    """Async HTTP probe for free model availability

    `probe_all()` shares one `httpx.AsyncClient` across the concurrent probes
    (connection pooling); a standalone `probe()` uses a one-shot client.
    `probe_all()` calls must not overlap on one instance (guarded).
    """

    #: Error prefixes worth an immediate retry (transient network faults).
    #: HTTP statuses — including 429 — are never retried.
    TRANSIENT_ERROR_PREFIXES = ("connect failed", "connect timeout", "read timeout")

    ZEN_ENDPOINT = "https://opencode.ai/zen/v1/chat/completions"

    #: Upper bound for response bodies parsed for `error.type` / `usage`
    #: (oversized bodies are treated as unparsable gateway output).
    MAX_PARSED_BODY_BYTES = 64 * 1024

    def __init__(self, timeout: float = 10.0, config: ProberConfig | None = None):
        self.timeout = timeout
        self.config = config or ProberConfig()
        self.log = logging.getLogger("prober")
        self._shared_client: httpx.AsyncClient | None = None
        self._batch_active = False
        # The Zen gateway requires x-opencode-session (MissingSessionID
        # otherwise). One id per prober instance: stable within a daemon's
        # life, unique across runs unless explicitly configured.
        self.session_id = self.config.session_id or f"ses_probe_{uuid.uuid4().hex}"

    def open(self) -> httpx.AsyncClient:
        """Ensure the persistent pooled client exists (idempotent).

        Long-lived owners (the daemon) call this once per cycle and
        `aclose()` at shutdown; one-shot users can skip both and let
        `probe_all()` manage a batch-scoped client.
        """
        if self._shared_client is None:
            self._shared_client = self._build_client()
        return self._shared_client

    async def aclose(self) -> None:
        """Close the persistent client, if any (idempotent)."""
        client, self._shared_client = self._shared_client, None
        self._batch_active = False
        if client is not None:
            await client.aclose()

    def _build_client(self) -> httpx.AsyncClient:
        """Create an httpx.AsyncClient honouring proxy / http2 / pool settings"""
        kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self.config.proxy:
            kwargs["proxy"] = self.config.proxy
        if self.config.http2:
            if importlib.util.find_spec("h2") is not None:
                kwargs["http2"] = True
            else:
                self.log.warning("prober.http2 requires the 'h2' package; falling back to HTTP/1.1")
        size = self.config.connection_pool_size
        kwargs["limits"] = httpx.Limits(max_connections=size, max_keepalive_connections=size)
        return httpx.AsyncClient(**kwargs)

    async def probe(self, model: str, headers: dict[str, str]) -> ProbeResult:
        """Probe a single model and return availability status"""
        if self._shared_client is not None:
            return await self._do_probe(model, headers, self._shared_client)
        client = self._build_client()
        try:
            return await self._do_probe(model, headers, client)
        finally:
            await client.aclose()

    async def _do_probe(
        self, model: str, headers: dict[str, str], client: httpx.AsyncClient
    ) -> ProbeResult:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": self.config.ping_message}],
            "max_tokens": self.config.max_tokens,
            "temperature": 0,
        }
        request_headers = {
            **headers,
            "x-opencode-session": self.session_id,
            **self.config.extra_headers,
        }

        attempts = 1 + max(0, self.config.max_retries)
        result: ProbeResult | None = None
        for attempt in range(attempts):
            result = await self._do_probe_once(model, request_headers, payload, client)
            if not _is_transient_error(result):
                return result
            if attempt + 1 < attempts:
                self.log.debug(
                    "Transient probe failure, retrying",
                    extra={"model": model, "attempt": attempt + 1, "error": result.error},
                )
        assert result is not None
        return result

    async def _do_probe_once(
        self,
        model: str,
        request_headers: dict[str, str],
        payload: dict[str, object],
        client: httpx.AsyncClient,
    ) -> ProbeResult:
        import time

        start = time.monotonic()
        timestamp = _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")

        try:
            resp = await client.post(self.config.endpoint, json=payload, headers=request_headers)
            latency = (time.monotonic() - start) * 1000

            if resp.status_code == 200:
                return ProbeResult(
                    model=model,
                    status="available",
                    http_status=200,
                    latency_ms=latency,
                    usage=_parse_usage(resp),
                    timestamp=timestamp,
                )
            elif resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                retry_after_int = _parse_retry_after(retry_after)
                return ProbeResult(
                    model=model,
                    status="rate_limited",
                    http_status=429,
                    retry_after=retry_after_int,
                    estimated_reset=self._estimate_reset(retry_after_int),
                    latency_ms=latency,
                    error_type=_parse_error_type(resp),
                    timestamp=timestamp,
                )
            else:
                return ProbeResult(
                    model=model,
                    status="error",
                    http_status=resp.status_code,
                    latency_ms=latency,
                    error=f"HTTP {resp.status_code}",
                    error_type=_parse_error_type(resp),
                    timestamp=timestamp,
                )

        except httpx.ConnectError as e:
            latency = (time.monotonic() - start) * 1000
            return ProbeResult(
                model=model,
                status="error",
                latency_ms=latency,
                error=f"connect failed: {e}",
                timestamp=timestamp,
            )
        except httpx.TimeoutException as e:
            latency = (time.monotonic() - start) * 1000
            # Connect timeouts point at the proxy/link; read timeouts at a
            # slow gateway — the distinction drives different remedies.
            kind = "connect timeout" if isinstance(e, httpx.ConnectTimeout) else "read timeout"
            return ProbeResult(
                model=model,
                status="error",
                latency_ms=latency,
                error=kind,
                timestamp=timestamp,
            )
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            return ProbeResult(
                model=model,
                status="error",
                latency_ms=latency,
                error=str(e),
                timestamp=timestamp,
            )

    async def probe_all(
        self,
        models: list[str],
        headers: dict[str, str],
        headers_by_model: dict[str, dict[str, str]] | None = None,
    ) -> list[ProbeResult]:
        """Probe multiple models concurrently

        `headers_by_model` optionally overrides the shared headers per model
        (used for per-account Authorization injection). All probes share one
        pooled `AsyncClient`.
        """
        import asyncio

        if not models:
            return []

        overrides = headers_by_model or {}
        if self._batch_active:
            raise RuntimeError("probe_all() calls must not overlap on one ModelProber")
        # Reuse a persistent client when the owner opened one; otherwise
        # fall back to a batch-scoped client closed below.
        owned = self._shared_client is None
        if owned:
            self._shared_client = self._build_client()
        self._batch_active = True
        try:
            tasks = [self.probe(model, overrides.get(model, headers)) for model in models]
            return await asyncio.gather(*tasks)
        finally:
            self._batch_active = False
            if owned:
                client, self._shared_client = self._shared_client, None
                assert client is not None  # just created above
                await client.aclose()

    @staticmethod
    def _estimate_reset(retry_after: int | None) -> int:
        """Seconds until the free-tier quota resets

        The Zen free tier resets daily at UTC midnight (the gateway's
        FreeUsageLimitError carries a retry-after header of exactly this
        value); when the header is missing, estimate seconds to UTC midnight.
        """
        if retry_after is not None:
            return retry_after
        return seconds_to_utc_midnight()


def seconds_to_utc_midnight() -> int:
    """Seconds until the next UTC midnight (the free-tier quota reset point)"""
    now = _dt.datetime.now(_dt.UTC)
    midnight = (now + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((midnight - now).total_seconds()))


def _parse_retry_after(raw: str | None) -> int | None:
    """Leniently parse a Retry-After header (delta-seconds, float, HTTP-date).

    Returns None when the header is missing or unparsable, so a weird header
    never downgrades a real 429 into a generic error.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    # Plain (or float) delta-seconds, the form the Zen gateway uses.
    try:
        return max(0, int(float(text)))
    except ValueError:
        pass
    # HTTP-date form (RFC 9110 §13.1.1): delay until that moment.
    from email.utils import parsedate_to_datetime

    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.UTC)
    delay = (moment - _dt.datetime.now(_dt.UTC)).total_seconds()
    return max(0, int(delay))


def _is_transient_error(result: ProbeResult) -> bool:
    """Whether a probe result reflects a transient network fault (retryable)."""
    return (
        result.status == "error"
        and result.http_status is None
        and isinstance(result.error, str)
        and result.error.startswith(ModelProber.TRANSIENT_ERROR_PREFIXES)
    )


def _parse_usage(resp: httpx.Response) -> dict[str, int] | None:
    """Extract per-request token cost from a 200 body (`usage` object).

    Returns None when absent or malformed — usage is informational, never
    load-bearing, so parsing is deliberately forgiving.
    """
    try:
        length = resp.headers.get("Content-Length")
        if length is not None and int(length) > ModelProber.MAX_PARSED_BODY_BYTES:
            return None
        body = resp.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    parsed: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            parsed[key] = int(value)
    return parsed or None


def _parse_error_type(resp: httpx.Response) -> str | None:
    """Extract error.type from a Zen error body: {"error": {"type": ...}}"""
    try:
        length = resp.headers.get("Content-Length")
        if length is not None and int(length) > ModelProber.MAX_PARSED_BODY_BYTES:
            return None
        body = resp.json()
    except Exception:
        return None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            err_type = err.get("type")
            if isinstance(err_type, str):
                return err_type
    return None
