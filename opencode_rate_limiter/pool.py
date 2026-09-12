"""Multi-account pool: auth resolution, rotation strategies, health tracking."""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

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

    def calculate_score(self) -> float:
        """Health score 0.0 - 1.0 (higher = healthier)"""

        # Success rate (50% weight)
        success_score = self.success_rate

        # Latency score (30% weight): 100ms = 1.0, >1000ms = 0.0
        latency_score = max(0.0, 1.0 - (self.avg_latency_ms - 100) / 900)

        # Recency score (20% weight): 0h = 0.0, 24h+ = 1.0
        hours_since_error = (
            (time.time() - self.last_error_time) / 3600 if self.last_error_time > 0 else 24
        )
        recency_score = min(1.0, hours_since_error / 24)

        return success_score * 0.5 + latency_score * 0.3 + recency_score * 0.2


@dataclass
class Account:
    """Account definition from config"""

    name: str
    auth_path: str | None = None
    env_var: str | None = None
    auth_json: str | None = None


def extract_access_token(auth: dict[str, Any]) -> str | None:
    """Extract a bearer token from an auth JSON structure

    Looks for a top-level "access_token"; falls back to one nested level deep
    (e.g. {"opencode": {"access_token": "..."}}).
    """
    token = auth.get("access_token")
    if isinstance(token, str) and token:
        return token
    for value in auth.values():
        if isinstance(value, dict):
            nested = value.get("access_token")
            if isinstance(nested, str) and nested:
                return nested
    return None


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
        self.log = logging.getLogger("pool")

    def get_next(self) -> Account | None:
        """Get next account based on strategy"""
        if not self.accounts:
            return None

        if self.config.strategy == "round_robin":
            return self._round_robin()
        elif self.config.strategy == "least_used":
            return self._least_used()
        elif self.config.strategy == "health":
            return self._healthiest()
        return None

    def _round_robin(self) -> Account:
        account = self.accounts[self._current_index % len(self.accounts)]
        self._current_index += 1
        return account

    def _least_used(self) -> Account:
        return min(self.accounts, key=lambda a: self.health[a.name].total_count)

    def _healthiest(self) -> Account:
        return max(self.accounts, key=lambda a: self.health[a.name].calculate_score())

    def mark_result(self, name: str, success: bool, latency_ms: float = 0.0) -> None:
        """Record probe result for health tracking"""

        h = self.health.get(name)
        if not h:
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

    def read_auth(self, account: Account) -> dict[str, Any] | None:
        """Read auth data from account source"""

        if account.auth_json:
            try:
                return cast("dict[str, Any]", json.loads(account.auth_json))
            except json.JSONDecodeError:
                return None

        if account.env_var:
            raw = os.environ.get(account.env_var)
            if raw:
                try:
                    return cast("dict[str, Any]", json.loads(raw))
                except json.JSONDecodeError:
                    return None
            return None

        if account.auth_path:
            path = Path(account.auth_path).expanduser()
            if path.exists():
                try:
                    with open(path, encoding="utf-8") as f:
                        return cast("dict[str, Any]", json.load(f))
                except (json.JSONDecodeError, OSError):
                    return None

        return None

    def resolve_token(self, account: Account) -> str | None:
        """Read auth data for the account and extract a bearer token"""
        auth = self.read_auth(account)
        if not auth:
            return None
        return extract_access_token(auth)

    def get_current(self) -> Account | None:
        """Get current active account"""
        if not self.accounts:
            return None
        return self.accounts[min(self._current_index, len(self.accounts) - 1)]
