"""Async availability probing of the Zen free-model endpoint."""

from __future__ import annotations

import datetime as _dt
import importlib.util
import logging
from dataclasses import dataclass
from typing import Any

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
            "timestamp": self.timestamp,
        }


class ModelProber:
    """Async HTTP probe for free model availability

    `probe_all()` shares one `httpx.AsyncClient` across the concurrent probes
    (connection pooling); a standalone `probe()` uses a one-shot client.
    """

    ZEN_ENDPOINT = "https://opencode.ai/zen/v1/chat/completions"

    def __init__(self, timeout: float = 10.0, config: ProberConfig | None = None):
        self.timeout = timeout
        self.config = config or ProberConfig()
        self.log = logging.getLogger("prober")
        self._shared_client: Any | None = None  # httpx.AsyncClient when set

    def _build_client(self) -> Any:
        """Create an httpx.AsyncClient honouring proxy / http2 / pool settings"""
        import httpx

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

    async def _do_probe(self, model: str, headers: dict[str, str], client: Any) -> ProbeResult:
        import time

        import httpx

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": self.config.ping_message}],
            "max_tokens": self.config.max_tokens,
            "temperature": 0,
        }
        request_headers = {**headers, **self.config.extra_headers}

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
                    timestamp=timestamp,
                )
            elif resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                retry_after_int = int(retry_after) if retry_after else None
                return ProbeResult(
                    model=model,
                    status="rate_limited",
                    http_status=429,
                    retry_after=retry_after_int,
                    estimated_reset=self._estimate_reset(retry_after_int),
                    latency_ms=latency,
                    timestamp=timestamp,
                )
            else:
                return ProbeResult(
                    model=model,
                    status="error",
                    http_status=resp.status_code,
                    latency_ms=latency,
                    error=f"HTTP {resp.status_code}",
                    timestamp=timestamp,
                )

        except httpx.TimeoutException:
            latency = (time.monotonic() - start) * 1000
            return ProbeResult(
                model=model,
                status="error",
                latency_ms=latency,
                error="timeout",
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

        overrides = headers_by_model or {}
        client = self._build_client()
        self._shared_client = client
        try:
            tasks = [self.probe(model, overrides.get(model, headers)) for model in models]
            return await asyncio.gather(*tasks)
        finally:
            self._shared_client = None
            await client.aclose()

    @staticmethod
    def _estimate_reset(retry_after: int | None) -> int | None:
        """Estimate reset time when Retry-After header is missing (silent limit)"""
        if retry_after is not None:
            return retry_after
        # Default estimate for silent limit: 60 seconds
        return 60
