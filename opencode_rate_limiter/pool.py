"""Multi-account pool: auth resolution, rotation strategies, health tracking."""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, cast

from .config import AccountPoolConfig


@dataclass
class AccountHealth:
    """Track health metrics for an account

    `results` is a sliding window of the most recent `window` probe outcomes;
    success_rate reflects the window when populated, and falls back to the
    cumulative counters otherwise (e.g. when counters are set directly).
    """

    name: str
    success_count: int = 0
    total_count: int = 0
    avg_latency_ms: float = 0.0
    last_error_time: float = 0.0
    last_success_time: float = 0.0
    consecutive_failures: int = 0
    key_limited_count: int = 0
    window: int = 100
    results: deque[bool] = field(default_factory=lambda: deque(maxlen=100))

    def __post_init__(self) -> None:
        if self.results.maxlen != self.window:
            self.results = deque(self.results, maxlen=self.window)

    @property
    def success_rate(self) -> float:
        if self.results:
            return sum(self.results) / len(self.results)
        return self.success_count / max(self.total_count, 1)

    DEFAULT_WEIGHTS: ClassVar[dict[str, float]] = {
        "success": 0.5,
        "latency": 0.3,
        "recency": 0.2,
    }

    def calculate_score(self, weights: dict[str, float] | None = None) -> float:
        """Health score 0.0 - 1.0 (higher = healthier)

        `weights` overrides the default success/latency/recency blend
        (`[account_pool].score_weights`).
        """
        w = weights or self.DEFAULT_WEIGHTS

        # Success rate
        success_score = self.success_rate

        # Latency score: 100ms = 1.0, >1000ms = 0.0 (clamped; a fresh
        # account with avg 0 must not score above 1.0).
        latency_score = min(1.0, max(0.0, 1.0 - (self.avg_latency_ms - 100) / 900))

        # Recency score: 0h = 0.0, 24h+ = 1.0
        hours_since_error = (
            (time.time() - self.last_error_time) / 3600 if self.last_error_time > 0 else 24
        )
        recency_score = min(1.0, hours_since_error / 24)

        return (
            success_score * w.get("success", 0.0)
            + latency_score * w.get("latency", 0.0)
            + recency_score * w.get("recency", 0.0)
        )


@dataclass
class Account:
    """Account definition from config

    `kind` is an optional override for the credential kind label
    ("oauth" | "api"); by default the kind is inferred from the auth payload.
    """

    name: str
    auth_path: str | None = None
    env_var: str | None = None
    auth_json: str | None = None
    kind: str | None = None


def _find_oauth_token(auth: dict[str, Any]) -> str | None:
    """Find an OAuth access token (top level or one nested level deep)"""
    for key in ("access_token", "access"):
        token = auth.get(key)
        if isinstance(token, str) and token:
            return token
    for value in auth.values():
        if isinstance(value, dict):
            for key in ("access_token", "access"):
                nested = value.get(key)
                if isinstance(nested, str) and nested:
                    return nested
    return None


def _find_api_key(auth: dict[str, Any]) -> str | None:
    """Find an API key (``{"type": "api", "key": ...}``, top level or nested;
    a bare top-level ``{"key": ...}`` is also accepted)"""
    raw_key = auth.get("key")
    if auth.get("type") == "api" and isinstance(raw_key, str):
        return raw_key
    if auth.get("type") is None and isinstance(raw_key, str):
        return raw_key
    for value in auth.values():
        if not isinstance(value, dict):
            continue
        nested_key = value.get("key")
        if value.get("type") == "api" and isinstance(nested_key, str):
            return nested_key
    return None


def extract_credential(auth: Any) -> tuple[str, str] | None:
    """Classify an auth payload into ``(kind, token)``

    kind is ``"api"`` when an API key is present (the keyRateLimiter
    dimension), ``"oauth"`` for OAuth access tokens, or None when neither
    shape matches. See extract_access_token for the recognized shapes.
    """
    if not isinstance(auth, dict):
        return None
    api_key = _find_api_key(auth)
    if api_key:
        return ("api", api_key)
    oauth = _find_oauth_token(auth)
    if oauth:
        return ("oauth", oauth)
    return None


def extract_access_token(auth: dict[str, Any]) -> str | None:
    """OAuth-compat alias: extract just the access token (kind-agnostic)"""
    credential = extract_credential(auth)
    return credential[1] if credential else None


def credential_fingerprint(token: str) -> str:
    """Redacted display form: first 6 + last 4 characters (never the full value)"""
    if len(token) <= 12:
        return "…" + token[-2:] if len(token) > 2 else "…"
    return f"{token[:6]}…{token[-4:]}"


ZEN_PROVIDER_KEY = "https://opencode.ai/zen"


def build_auth_payload(auth: dict[str, Any]) -> dict[str, Any]:
    """Normalize an auth payload into opencode's provider-keyed auth.json shape

    - provider-keyed snapshots pass through unchanged
    - single entries ({type: oauth|api, ...}) are wrapped under the Zen key
    - bare token payloads ({"access": ...} / {"key": ...}) are re-typed
    """
    is_provider_keyed = (
        bool(auth) and auth.get("type") is None and all(isinstance(v, dict) for v in auth.values())
    )
    if is_provider_keyed:
        return auth
    credential = extract_credential(auth)
    if credential and credential[0] == "api":
        return {ZEN_PROVIDER_KEY: {"type": "api", "key": credential[1]}}
    if auth.get("type") in ("oauth", "api", "wellknown"):
        return {ZEN_PROVIDER_KEY: auth}
    if credential:
        return {ZEN_PROVIDER_KEY: {"type": "oauth", "access": credential[1]}}
    return auth


class AccountPool:
    """Multi-account rotation manager"""

    def __init__(self, config: AccountPoolConfig):
        self.config = config
        self.accounts = [Account(**acc) for acc in config.accounts]
        self.health: dict[str, AccountHealth] = {
            acc.name: AccountHealth(name=acc.name, window=config.health_window)
            for acc in self.accounts
        }
        self._current_index = 0
        self._last_served: Account | None = None
        # Auth read cache: ("path", str) -> ((mtime_ns, size), data),
        # ("env", var) -> (raw_value, data), ("json", payload) -> data.
        # Files invalidate on mtime/size change; env/inline on value change.
        self._auth_cache: dict[tuple[str, str], tuple[Any, Any]] = {}
        self.log = logging.getLogger("pool")

    def get_next(self, record: bool = True) -> Account | None:
        """Get next account based on strategy.

        When `record` is true (default) the pick is stored as `last_served`;
        pass False for speculative picks (e.g. skipping cooled accounts).
        """
        if not self.accounts:
            return None

        if self.config.strategy == "round_robin":
            account: Account | None = self._round_robin()
        elif self.config.strategy == "least_used":
            account = self._least_used()
        elif self.config.strategy == "health":
            account = self._healthiest()
        else:
            return None
        if record:
            self._last_served = account
        return account

    def _round_robin(self) -> Account:
        account = self.accounts[self._current_index % len(self.accounts)]
        self._current_index += 1
        return account

    def _least_used(self) -> Account:
        return min(self.accounts, key=lambda a: self.health[a.name].total_count)

    def _healthiest(self) -> Account:
        return max(
            self.accounts,
            key=lambda a: self.health[a.name].calculate_score(self.config.score_weights),
        )

    def mark_result(
        self,
        name: str,
        success: bool,
        latency_ms: float = 0.0,
        error_type: str | None = None,
        error_kind: str | None = None,
    ) -> None:
        """Record probe result for health tracking

        Attribution rules: a ``FreeUsageLimitError`` is an IP-level failure and
        is NOT counted against the credential; a ``RateLimitError`` counts as a
        key-dimension failure (``key_limited_count``). Transient transport,
        reasoning-replay (poisoned session) and upstream failures are also NOT
        credential failures — rotating on them makes things worse.
        """

        h = self.health.get(name)
        if not h:
            return

        # IP-level limits and non-credential failures never pollute health.
        # error_kind (when present) is authoritative; error_type covers older
        # callers and normalized prober types.
        skip_types = frozenset(
            {
                "FreeUsageLimitError",
                "ReasoningReplayError",
                "UpstreamError",
                "TransientTransport",
            }
        )
        skip_kinds = frozenset({"transient_transport", "reasoning_replay", "upstream"})
        if not success and (error_type in skip_types or (error_kind or "") in skip_kinds):
            self.log.debug(
                "Skipping health mark for %s: %s/%s is not a credential failure",
                name,
                error_kind or "-",
                error_type or "-",
            )
            return

        h.total_count += 1
        now = time.time()
        h.results.append(success)

        if success:
            h.success_count += 1
            h.consecutive_failures = 0
            h.last_success_time = now
            # Exponential moving average for latency
            if h.avg_latency_ms == 0:
                h.avg_latency_ms = latency_ms
            else:
                h.avg_latency_ms = h.avg_latency_ms * 0.8 + latency_ms * 0.2
        else:
            h.consecutive_failures += 1
            h.last_error_time = now
            if error_type == "RateLimitError":
                h.key_limited_count += 1

    def read_auth(self, account: Account) -> dict[str, Any] | None:
        """Read auth data from account source (with change-aware caching)"""

        if account.auth_json:
            key = ("json", account.auth_json)
            cached = self._auth_cache.get(key)
            if cached is not None:
                return cast("dict[str, Any] | None", cached[1])
            try:
                data: dict[str, Any] | None = cast("dict[str, Any]", json.loads(account.auth_json))
            except json.JSONDecodeError:
                data = None
            self._auth_cache[key] = (None, data)
            return data

        if account.env_var:
            raw = os.environ.get(account.env_var)
            if not raw:
                return None
            key = ("env", account.env_var)
            cached = self._auth_cache.get(key)
            if cached is not None and cached[0] == raw:
                return cast("dict[str, Any] | None", cached[1])
            try:
                data = cast("dict[str, Any]", json.loads(raw))
            except json.JSONDecodeError:
                data = None
            self._auth_cache[key] = (raw, data)
            return data

        if account.auth_path:
            path = Path(os.path.expandvars(str(Path(account.auth_path).expanduser())))
            if not path.exists():
                return None
            try:
                stat = path.stat()
                freshness = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                return None
            key = ("path", str(path))
            cached = self._auth_cache.get(key)
            if cached is not None and cached[0] == freshness:
                return cast("dict[str, Any] | None", cached[1])
            try:
                with open(path, encoding="utf-8") as f:
                    data = cast("dict[str, Any]", json.load(f))
            except (json.JSONDecodeError, OSError):
                return None
            self._auth_cache[key] = (freshness, data)
            return data

        return None

    def invalidate_auth_cache(self) -> None:
        """Drop all cached auth payloads (tests, credential rotation)."""
        self._auth_cache.clear()

    def resolve_credential(self, account: Account) -> tuple[str, str] | None:
        """Read auth data and classify it as (kind, token)

        An explicit ``account.kind`` overrides the inferred kind label.
        """
        auth = self.read_auth(account)
        if not auth:
            return None
        credential = extract_credential(auth)
        if credential and account.kind and credential[0] != account.kind:
            return (account.kind, credential[1])
        return credential

    def resolve_token(self, account: Account) -> str | None:
        """Compat alias: extract just the bearer token"""
        credential = self.resolve_credential(account)
        return credential[1] if credential else None

    def note_served(self, account: Account) -> None:
        """Record an account as most recently served (for peek-style picks)."""
        self._last_served = account

    def get_current(self) -> Account | None:
        """Get the account that most recently served a request.

        Falls back to the first account before anything was served (and None
        for an empty pool). Prefer `last_served` for new code.
        """
        if not self.accounts:
            return None
        return self._last_served or self.accounts[0]

    @property
    def last_served(self) -> Account | None:
        """The account returned by the most recent `get_next()` call, if any."""
        return self._last_served
