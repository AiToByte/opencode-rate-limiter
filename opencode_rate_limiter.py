#!/usr/bin/env python3
"""
OpenCode Rate Limiter - Phase 5 Implementation
配置管理、跨平台路径解析、结构化日志、模型探测、账号轮换、清理、守护进程模式与 Shell 补全
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as _dt
import json
import logging
import os
import sys
import tomllib
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

try:
    import tomli_w
except ImportError:
    tomli_w: Any = None  # type: ignore[no-redef]

from platformdirs import user_config_dir, user_state_dir

__version__ = "0.1.0"


# =============================================================================
# Data Classes: Configuration
# =============================================================================

FREE_MODELS = [
    "deepseek-v4-flash-free",
    "nemotron-3-ultra-free",
    "big-pickle",
    "mimo-v2.5-free",
    "hy3-free",
    "laguna-s-2.1-free",
    "ling-3.0-flash-fin-free",
    "nemotron-3.5-lightning-free",
]


@dataclass
class DaemonConfig:
    interval_seconds: int = 30
    models: list[str] = field(default_factory=lambda: list(FREE_MODELS))
    probe_timeout_seconds: float = 10.0
    auto_cleanup_on_429: bool = True

    def validate(self) -> None:
        if self.interval_seconds < 5:
            raise ValueError(f"interval_seconds must be >= 5, got {self.interval_seconds}")
        if self.probe_timeout_seconds <= 0:
            raise ValueError(f"probe_timeout_seconds must be > 0, got {self.probe_timeout_seconds}")
        if not self.models:
            raise ValueError("models list cannot be empty")


@dataclass
class AccountPoolConfig:
    accounts: list[dict[str, Any]] = field(default_factory=list)
    strategy: Literal["round_robin", "least_used", "health"] = "health"
    health_window: int = 100

    def validate(self) -> None:
        valid_strategies = {"round_robin", "least_used", "health"}
        if self.strategy not in valid_strategies:
            raise ValueError(f"strategy must be one of {valid_strategies}, got {self.strategy}")
        if self.health_window < 1:
            raise ValueError(f"health_window must be >= 1, got {self.health_window}")
        for i, acc in enumerate(self.accounts):
            if not isinstance(acc, dict):
                raise ValueError(f"accounts[{i}] must be a dict")
            if "name" not in acc:
                raise ValueError(f"accounts[{i}] missing required 'name' field")
            # At least one auth source
            has_source = any(k in acc for k in ("auth_path", "env_var", "auth_json"))
            if not has_source:
                raise ValueError(
                    f"accounts[{i}] missing auth source (auth_path, env_var, or auth_json)"
                )


@dataclass
class ProberConfig:
    """Probe request customization (endpoint, payload, extra headers, proxy)"""

    endpoint: str = "https://opencode.ai/zen/v1/chat/completions"
    ping_message: str = "ping"
    max_tokens: int = 1
    extra_headers: dict[str, str] = field(default_factory=dict)
    proxy: str | None = None

    def validate(self) -> None:
        if not self.endpoint.startswith(("http://", "https://")):
            raise ValueError(f"prober.endpoint must be an http(s) URL, got {self.endpoint}")
        if self.max_tokens < 1:
            raise ValueError(f"prober.max_tokens must be >= 1, got {self.max_tokens}")


@dataclass
class HeadersConfig:
    user_agent: str = "opencode/{version}"
    x_opencode_client: str = "opencode-cli"
    x_opencode_version: str = "{version}"


@dataclass
class CleanupConfig:
    cache_dirs: list[str] = field(default_factory=list)
    state_files: list[str] = field(default_factory=list)
    preserve_config: bool = True

    def validate(self) -> None:
        if not self.preserve_config:
            raise ValueError("preserve_config must be true (protects user config.json)")


@dataclass
class Config:
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    account_pool: AccountPoolConfig = field(default_factory=AccountPoolConfig)
    prober: ProberConfig = field(default_factory=ProberConfig)
    headers: HeadersConfig = field(default_factory=HeadersConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)

    # Runtime: expanded paths
    _expanded_cache_dirs: list[Path] = field(default_factory=list, init=False, repr=False)
    _expanded_state_files: list[Path] = field(default_factory=list, init=False, repr=False)

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """Load config with precedence: CLI > OPENCODE_RATE_LIMITER_CONFIG env > file > defaults"""
        config = cls()

        # 1. Resolve config file path: CLI > env var > platform default
        if path is None:
            env_path = os.environ.get("OPENCODE_RATE_LIMITER_CONFIG")
            if env_path:
                path = Path(os.path.expandvars(env_path)).expanduser()

        # 2. Load from file (lowest precedence)
        file_config = cls._load_from_file(path)
        if file_config:
            config = cls._merge(config, file_config)

        # 3. Load from environment variables
        env_config = cls._load_from_env()
        if env_config:
            config = cls._merge(config, env_config)

        # 4. Validate
        config.validate()

        # 5. Expand paths
        config._expand_paths()

        return config

    @classmethod
    def _load_from_file(cls, path: Path | None) -> dict[str, Any] | None:
        """Load TOML config from file"""
        if path is None:
            # Default location
            config_dir = Path(user_config_dir("opencode-rate-limiter"))
            path = config_dir / "config.toml"

        if not path.exists():
            return None

        try:
            with open(path, "rb") as f:
                return tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"Invalid TOML in {path}: {e}") from e

    @classmethod
    def _load_from_env(cls) -> dict[str, Any] | None:
        """Load config from environment variables

        Naming convention: OPENCODE_RATE_LIMITER_<SECTION>_<KEY>
        Section and key names use double underscore as separator.
        Examples:
            OPENCODE_RATE_LIMITER_DAEMON__INTERVAL_SECONDS=60
            OPENCODE_RATE_LIMITER_ACCOUNT_POOL__STRATEGY=round_robin
            OPENCODE_RATE_LIMITER_DAEMON__AUTO_CLEANUP_ON_429=false
        """
        prefix = "OPENCODE_RATE_LIMITER_"
        env_config: dict[str, Any] = {}

        for key, value in os.environ.items():
            if not key.startswith(prefix):
                continue

            suffix = key[len(prefix) :]
            # Double underscore separates section from key
            if "__" not in suffix:
                continue

            parts = suffix.split("__", 1)
            section = parts[0].lower()
            field_name = parts[1].lower()

            section_dict = env_config.setdefault(section, {})
            section_dict[field_name] = cls._parse_env_value(value)

        return env_config if env_config else None

    @staticmethod
    def _parse_env_value(value: str) -> Any:
        """Parse environment variable value to appropriate type"""
        # Boolean
        if value.lower() in ("true", "false"):
            return value.lower() == "true"
        # Integer
        try:
            return int(value)
        except ValueError:
            pass
        # Float
        try:
            return float(value)
        except ValueError:
            pass
        # List (comma-separated)
        if "," in value:
            return [v.strip() for v in value.split(",")]
        # String
        return value

    @staticmethod
    def _coerce_value(current_value: Any, new_value: Any) -> Any:
        """Coerce new_value to match the type of current_value"""
        if isinstance(current_value, bool):
            if isinstance(new_value, str):
                return new_value.lower() in ("true", "1", "yes")
            return bool(new_value)
        if isinstance(current_value, int):
            if isinstance(new_value, str):
                return int(new_value)
            return int(new_value)
        if isinstance(current_value, float):
            if isinstance(new_value, str):
                return float(new_value)
            return float(new_value)
        return new_value

    @classmethod
    def _merge(cls, base: Config, override: dict[str, Any]) -> Config:
        """Merge override dict into base config"""
        result = cls(
            daemon=base.daemon,
            account_pool=base.account_pool,
            prober=base.prober,
            headers=base.headers,
            cleanup=base.cleanup,
        )

        # Merge daemon
        if "daemon" in override:
            for k, v in override["daemon"].items():
                if hasattr(result.daemon, k):
                    current = getattr(result.daemon, k)
                    setattr(result.daemon, k, cls._coerce_value(current, v))

        # Merge account_pool
        if "account_pool" in override:
            for k, v in override["account_pool"].items():
                if hasattr(result.account_pool, k):
                    current = getattr(result.account_pool, k)
                    setattr(result.account_pool, k, cls._coerce_value(current, v))

        # Merge prober
        if "prober" in override:
            for k, v in override["prober"].items():
                if hasattr(result.prober, k):
                    current = getattr(result.prober, k)
                    setattr(result.prober, k, cls._coerce_value(current, v))

        # Merge headers
        if "headers" in override:
            for k, v in override["headers"].items():
                if hasattr(result.headers, k):
                    current = getattr(result.headers, k)
                    setattr(result.headers, k, cls._coerce_value(current, v))

        # Merge cleanup
        if "cleanup" in override:
            for k, v in override["cleanup"].items():
                if hasattr(result.cleanup, k):
                    current = getattr(result.cleanup, k)
                    setattr(result.cleanup, k, cls._coerce_value(current, v))

        return result

    def validate(self) -> None:
        self.daemon.validate()
        self.account_pool.validate()
        self.prober.validate()
        self.cleanup.validate()

    def _expand_paths(self) -> None:
        """Expand ~, env vars, XDG paths in cache_dirs and state_files"""
        self._expanded_cache_dirs = [self._expand_path(p) for p in self.cleanup.cache_dirs]
        self._expanded_state_files = [self._expand_path(p) for p in self.cleanup.state_files]

    @staticmethod
    def _expand_path(path_str: str) -> Path:
        """Expand a path string with ~, env vars, XDG"""
        # Expand ~ and $HOME
        expanded = Path(path_str).expanduser()
        # Expand environment variables
        expanded_str = os.path.expandvars(str(expanded))
        return Path(expanded_str)

    def get_cache_dirs(self) -> list[Path]:
        """Get expanded cache directories (includes OpenCode native paths)"""
        dirs = self._expanded_cache_dirs.copy()
        # Add OpenCode native cache directories
        dirs.extend(get_opencode_native_cache_dirs())
        # Deduplicate preserving order
        seen = set()
        unique = []
        for d in dirs:
            if d not in seen:
                seen.add(d)
                unique.append(d)
        return unique

    def get_state_files(self) -> list[Path]:
        """Get expanded state files (includes OpenCode native paths)"""
        files = self._expanded_state_files.copy()
        files.extend(get_opencode_native_state_files())
        seen = set()
        unique = []
        for f in files:
            if f not in seen:
                seen.add(f)
                unique.append(f)
        return unique

    def save(self, path: Path) -> None:
        """Save config to TOML file"""
        if tomli_w is None:
            raise RuntimeError("tomli_w not installed, cannot save config")

        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "daemon": {
                "interval_seconds": self.daemon.interval_seconds,
                "models": self.daemon.models,
                "probe_timeout_seconds": self.daemon.probe_timeout_seconds,
                "auto_cleanup_on_429": self.daemon.auto_cleanup_on_429,
            },
            "account_pool": {
                "accounts": self.account_pool.accounts,
                "strategy": self.account_pool.strategy,
            },
            "prober": {
                "endpoint": self.prober.endpoint,
                "ping_message": self.prober.ping_message,
                "max_tokens": self.prober.max_tokens,
                "extra_headers": self.prober.extra_headers,
                # tomli_w cannot serialize None; omit proxy when unset
                **({"proxy": self.prober.proxy} if self.prober.proxy else {}),
            },
            "headers": {
                "user_agent": self.headers.user_agent,
                "x_opencode_client": self.headers.x_opencode_client,
                "x_opencode_version": self.headers.x_opencode_version,
            },
            "cleanup": {
                "cache_dirs": self.cleanup.cache_dirs,
                "state_files": self.cleanup.state_files,
                "preserve_config": self.cleanup.preserve_config,
            },
        }

        with open(path, "wb") as f:
            tomli_w.dump(data, f)


# =============================================================================
# Path Resolution: OpenCode Native Paths
# =============================================================================


def get_opencode_config_dirs() -> list[Path]:
    """Get all possible OpenCode config directories (cross-platform)"""
    dirs = []

    # Standard ~/.opencode
    dirs.append(Path.home() / ".opencode")

    # XDG / platformdirs
    dirs.append(Path(user_config_dir("opencode")))
    dirs.append(Path(user_state_dir("opencode")))

    # macOS
    if sys.platform == "darwin":
        dirs.append(Path.home() / "Library" / "Application Support" / "opencode")

    # Windows
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            dirs.append(Path(appdata) / "opencode")
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            dirs.append(Path(localappdata) / "opencode")

    # Linux XDG
    else:
        xdg_config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        dirs.append(Path(xdg_config) / "opencode")
        xdg_state = os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
        dirs.append(Path(xdg_state) / "opencode")

    # Deduplicate
    seen = set()
    unique = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    return unique


def get_opencode_native_cache_dirs() -> list[Path]:
    """Get OpenCode native cache directories"""
    cache_dirs = []
    for config_dir in get_opencode_config_dirs():
        cache_dirs.append(config_dir / "cache")
    return cache_dirs


def get_opencode_native_state_files() -> list[Path]:
    """Get OpenCode native state files"""
    state_files = []
    for config_dir in get_opencode_config_dirs():
        state_files.append(config_dir / "state.json")
    return state_files


def get_opencode_auth_files() -> list[Path]:
    """Get OpenCode auth.json files"""
    auth_files = []
    for config_dir in get_opencode_config_dirs():
        auth_files.append(config_dir / "auth.json")
    return auth_files


def get_opencode_version() -> str:
    """Detect OpenCode CLI version

    `OPENCODE_VERSION` env var overrides detection (useful for containers or
    when the opencode binary is not on PATH).
    """
    override = os.environ.get("OPENCODE_VERSION")
    if override:
        return override
    try:
        import subprocess
        from shutil import which

        binary = which("opencode")
        if not binary:
            return "unknown"
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and (result.stdout.strip() or result.stderr.strip()):
            return result.stdout.strip() or result.stderr.strip()
    except Exception:
        pass
    return "unknown"


# =============================================================================
# Phase 2: HeaderInjector
# =============================================================================


class HeaderInjector:
    """Generate OpenCode official CLI compatible request headers"""

    ZEN_ENDPOINT = "https://opencode.ai/zen/v1/chat/completions"

    def __init__(self, config: HeadersConfig, version: str = "unknown"):
        self.config = config
        self.version = version

    def build_headers(self, model: str | None = None, token: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self.config.user_agent.format(version=self.version),
            "x-opencode-client": self.config.x_opencode_client,
            "x-opencode-version": self.config.x_opencode_version.format(version=self.version),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if model:
            headers["x-model"] = model
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def to_env_export(self, model: str | None = None) -> str:
        """Generate shell export statements for eval"""
        h = self.build_headers(model)
        lines = []
        for k, v in h.items():
            env_key = k.upper().replace("-", "_")
            lines.append(f'export {env_key}="{v}"')
        return "\n".join(lines)

    def to_curl_args(self, model: str | None = None) -> str:
        """Generate curl header arguments"""
        h = self.build_headers(model)
        args = []
        for k, v in h.items():
            args.append(f'-H "{k}: {v}"')
        return " ".join(args)


# =============================================================================
# Phase 2: ModelProber
# =============================================================================


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
    """Async HTTP probe for free model availability"""

    ZEN_ENDPOINT = "https://opencode.ai/zen/v1/chat/completions"

    def __init__(self, timeout: float = 10.0, config: ProberConfig | None = None):
        self.timeout = timeout
        self.config = config or ProberConfig()
        self.log = logging.getLogger("prober")

    async def probe(self, model: str, headers: dict[str, str]) -> ProbeResult:
        """Probe a single model and return availability status"""
        import time

        import httpx

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": self.config.ping_message}],
            "max_tokens": self.config.max_tokens,
            "temperature": 0,
        }
        request_headers = {**headers, **self.config.extra_headers}
        client_kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self.config.proxy:
            client_kwargs["proxy"] = self.config.proxy

        start = time.monotonic()
        timestamp = _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")

        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.post(
                    self.config.endpoint, json=payload, headers=request_headers
                )
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
        (used for per-account Authorization injection).
        """
        import asyncio

        overrides = headers_by_model or {}
        tasks = [self.probe(model, overrides.get(model, headers)) for model in models]
        return await asyncio.gather(*tasks)

    @staticmethod
    def _estimate_reset(retry_after: int | None) -> int | None:
        """Estimate reset time when Retry-After header is missing (silent limit)"""
        if retry_after is not None:
            return retry_after
        # Default estimate for silent limit: 60 seconds
        return 60


# =============================================================================
# Phase 2: AccountPool
# =============================================================================


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
        import time

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
        import time

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
        import json

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


# =============================================================================
# Phase 2: CleanupManager
# =============================================================================


@dataclass
class CleanupResult:
    """Result of a cleanup operation"""

    cleared_count: int = 0
    errors: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cleared_count": self.cleared_count,
            "errors": self.errors,
            "details": self.details,
        }


class CleanupManager:
    """File system cleanup for rate limit state, auth, and cache"""

    def __init__(self, config: CleanupConfig):
        self.config = config
        self.log = logging.getLogger("cleanup")

    def reset_rate_limit_state(
        self, state_files: list[Path] | None = None, dry_run: bool = False
    ) -> CleanupResult:
        """Remove rate limit lock files and state.json"""
        result = CleanupResult()
        targets = state_files or get_opencode_native_state_files()

        for path in targets:
            # Also look for rate_limit lock files in parent directories
            parent = path.parent
            if parent.is_dir():
                for lock_file in parent.glob("*rate_limit*.json"):
                    if dry_run:
                        result.details.append(f"DRY RUN: Would remove {lock_file}")
                        result.cleared_count += 1
                        continue
                    try:
                        lock_file.unlink()
                        result.cleared_count += 1
                        result.details.append(f"Removed {lock_file.name}")
                        self.log.info("Removed rate limit lock: %s", lock_file)
                    except Exception as e:
                        result.errors.append(f"Failed to remove {lock_file}: {e}")
                        self.log.warning("Failed to remove %s: %s", lock_file, e)

            if path.name == "state.json" and path.exists():
                if dry_run:
                    result.details.append(f"DRY RUN: Would remove {path}")
                    result.cleared_count += 1
                    continue
                try:
                    path.unlink()
                    result.cleared_count += 1
                    result.details.append(f"Removed {path.name}")
                    self.log.info("Removed state file: %s", path)
                except Exception as e:
                    result.errors.append(f"Failed to remove {path}: {e}")
                    self.log.warning("Failed to remove %s: %s", path, e)

        return result

    def rotate_auth_tokens(
        self, auth_files: list[Path] | None = None, dry_run: bool = False
    ) -> CleanupResult:
        """Backup and clear access_token from auth files"""
        import json

        result = CleanupResult()
        targets = auth_files or get_opencode_auth_files()

        for auth_file in targets:
            if not auth_file.exists():
                continue

            if dry_run:
                result.details.append(f"DRY RUN: Would rotate {auth_file}")
                result.cleared_count += 1
                continue

            try:
                # Backup
                backup_path = auth_file.with_suffix(".json.bak")
                import shutil

                shutil.copy(auth_file, backup_path)
                result.details.append(f"Backed up to {backup_path.name}")

                # Clear token
                with open(auth_file, "r+", encoding="utf-8") as f:
                    data = json.load(f)
                    if "access_token" in data:
                        data["access_token"] = ""
                    if "rate_limited_until" in data:
                        del data["rate_limited_until"]
                    f.seek(0)
                    json.dump(data, f, indent=2)
                    f.truncate()

                result.cleared_count += 1
                result.details.append(f"Cleared tokens in {auth_file.name}")
                self.log.info("Rotated auth tokens: %s", auth_file)

            except json.JSONDecodeError:
                try:
                    auth_file.unlink()
                    result.details.append(f"Removed corrupt {auth_file.name}")
                    self.log.warning("Removed corrupt auth file: %s", auth_file)
                except Exception as e:
                    result.errors.append(f"Failed to remove corrupt {auth_file}: {e}")
            except Exception as e:
                result.errors.append(f"Failed to rotate {auth_file}: {e}")
                self.log.warning("Failed to rotate %s: %s", auth_file, e)

        return result

    def purge_cache(
        self, cache_dirs: list[Path] | None = None, dry_run: bool = False
    ) -> CleanupResult:
        """Remove cache directories"""
        import shutil

        result = CleanupResult()
        targets = cache_dirs or get_opencode_native_cache_dirs()

        for cache_path in targets:
            if not cache_path.exists():
                continue

            if dry_run:
                result.details.append(f"DRY RUN: Would remove {cache_path}")
                result.cleared_count += 1
                continue

            try:
                if cache_path.is_dir():
                    shutil.rmtree(cache_path)
                    cache_path.mkdir(parents=True, exist_ok=True)
                    result.cleared_count += 1
                    result.details.append(f"Purged {cache_path}")
                    self.log.info("Purged cache: %s", cache_path)
            except Exception as e:
                result.errors.append(f"Failed to purge {cache_path}: {e}")
                self.log.warning("Failed to purge %s: %s", cache_path, e)

        return result

    def full_cleanup(self, dry_run: bool = False, include_cache: bool = True) -> CleanupResult:
        """Run cleanup operations

        `include_cache=False` limits the pass to rate-limit locks + state.json
        + auth token reset (the `quick` profile); cache purging is the `deep`
        profile.
        """
        result = CleanupResult()

        r1 = self.reset_rate_limit_state(dry_run=dry_run)
        result.cleared_count += r1.cleared_count
        result.errors.extend(r1.errors)
        result.details.extend(r1.details)

        r2 = self.rotate_auth_tokens(dry_run=dry_run)
        result.cleared_count += r2.cleared_count
        result.errors.extend(r2.errors)
        result.details.extend(r2.details)

        if include_cache:
            r3 = self.purge_cache(dry_run=dry_run)
            result.cleared_count += r3.cleared_count
            result.errors.extend(r3.errors)
            result.details.extend(r3.details)

        return result


# =============================================================================
# Logging System
# =============================================================================


class JSONFormatter(logging.Formatter):
    """JSON Lines log formatter"""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add extra fields
        for key, value in record.__dict__.items():
            if key not in {
                "name",
                "msg",
                "args",
                "created",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "message",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "thread",
                "threadName",
                "exc_info",
                "exc_text",
                "stack_info",
            }:
                log_data[key] = value

        return json.dumps(log_data, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    """Human-readable log formatter"""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", datefmt="%H:%M:%S"
        )


def setup_logging(level: int, json_output: bool) -> None:
    # Windows GBK consoles cannot encode some log characters; replace instead
    # of raising UnicodeEncodeError mid-log.
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                with contextlib.suppress(Exception):
                    stream.reconfigure(errors="replace")

    handler = logging.StreamHandler(sys.stderr)

    if json_output:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(HumanFormatter())

    logging.root.setLevel(level)
    logging.root.handlers = [handler]

    # Suppress noisy loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def level_from_args(args: argparse.Namespace) -> int:
    if args.quiet:
        return logging.ERROR
    if args.verbose >= 2:
        return logging.DEBUG
    if args.verbose >= 1:
        return logging.INFO
    return logging.WARNING


# =============================================================================
# CLI Argument Parser
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opencode-rate-limiter",
        description="OpenCode 免费模型限流缓解与账号池管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  opencode-rate-limiter quick                    # Quick rate limit relief
  opencode-rate-limiter deep                     # Deep cleanup
  opencode-rate-limiter probe all --json         # Probe all models
  opencode-rate-limiter headers --export         # Export headers for curl
  opencode-rate-limiter daemon --interval 60     # Run as daemon
  opencode-rate-limiter check --json             # Health check
        """,
    )
    parser.add_argument("--config", type=Path, help="配置文件路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式日志")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="详细输出 (-v, -vv)")
    parser.add_argument("-q", "--quiet", action="store_true", help="仅错误输出")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不修改文件")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", required=True, help="子命令")

    # Shared flags, available both before and after the subcommand.
    # SUPPRESS default lets parent-parser values survive when the child omits them.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="输出 JSON 格式"
    )
    common.add_argument(
        "--dry-run", action="store_true", default=argparse.SUPPRESS, help="预览模式，不修改文件"
    )

    def add_sub(name: str, help_text: str) -> argparse.ArgumentParser:
        """Register a subcommand, mirroring `help` into `description` so both -h
        output and completion generation can read the one-line summary."""
        return sub.add_parser(name, parents=[common], help=help_text, description=help_text)

    add_sub("quick", "快速解除限流（清理退避锁 + 重置 Token）")
    add_sub("deep", "深度清理（+ 清除缓存 + 强制重新登录）")

    probe_p = add_sub("probe", "探测模型可用性")
    probe_p.add_argument("model", nargs="?", default="all", help="模型名称或 all")

    headers_p = add_sub("headers", "输出官方 CLI 兼容请求头")
    headers_p.add_argument("--model", help="目标模型（可选）")
    headers_p.add_argument("--export", action="store_true", help="输出 shell export 格式")

    rotate_p = add_sub("rotate", "手动轮换账号池")
    rotate_p.add_argument(
        "--strategy", choices=["round_robin", "least_used", "health"], default="health"
    )

    add_sub("check", "健康检查聚合输出")

    daemon_p = add_sub("daemon", "后台守护进程模式")
    daemon_p.add_argument("--interval", type=int, default=None, help="探测间隔（秒，最小 5）")
    daemon_p.add_argument("--models", type=str, help="逗号分隔的模型列表")

    add_sub("generate-systemd", "生成 systemd 服务文件")
    add_sub("generate-launchd", "生成 launchd plist")
    add_sub("generate-task", "生成 Windows 任务计划 XML")

    gen_config_p = add_sub("generate-config", "生成默认配置文件（已存在时需 --force 覆盖）")
    gen_config_p.add_argument("--force", action="store_true", help="覆盖已存在的配置文件")

    completions_p = add_sub("completions", "生成 shell 补全脚本 (bash/zsh/fish)")
    completions_p.add_argument("shell", choices=["bash", "zsh", "fish"], help="目标 shell")

    return parser


# =============================================================================
# Command Handlers
# =============================================================================


def _print_cleanup_result(result: CleanupResult) -> None:
    for d in result.details:
        print(f"  {d}")
    if result.errors:
        for e in result.errors:
            print(f"  ERROR: {e}")
    print(f"\nDone: {result.cleared_count} items processed, {len(result.errors)} errors")


async def cmd_quick(config: Config, args: argparse.Namespace) -> int:
    log = logging.getLogger("cmd.quick")
    log.info("Starting quick cleanup", extra={"dry_run": args.dry_run})

    cleanup = CleanupManager(config.cleanup)
    # quick profile: rate-limit locks + state.json + auth token reset, no cache purge
    result = cleanup.full_cleanup(dry_run=args.dry_run, include_cache=False)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_cleanup_result(result)

    return 0 if not result.errors else 1


async def cmd_deep(config: Config, args: argparse.Namespace) -> int:
    log = logging.getLogger("cmd.deep")
    log.info("Starting deep cleanup", extra={"dry_run": args.dry_run})

    cleanup = CleanupManager(config.cleanup)
    # deep profile: quick + cache purge; tokens are cleared so a re-login is expected
    result = cleanup.full_cleanup(dry_run=args.dry_run, include_cache=True)

    if args.json:
        data = result.to_dict()
        data["relogin_hint"] = "run `opencode login` to refresh the cleared tokens"
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        _print_cleanup_result(result)
        print("\nTokens were cleared - run `opencode login` to authenticate again.")

    return 0 if not result.errors else 1


async def cmd_probe(config: Config, args: argparse.Namespace) -> int:
    log = logging.getLogger("cmd.probe")
    version = get_opencode_version()
    injector = HeaderInjector(config.headers, version)
    prober = ModelProber(config.daemon.probe_timeout_seconds, config.prober)

    models = config.daemon.models if args.model == "all" else [args.model]
    headers = injector.build_headers()

    # Per-model account rotation: each probed model carries the auth token of
    # the account selected for it (round-robin/least_used/health), so results
    # feed back into the pool's health tracking.
    pool = AccountPool(config.account_pool) if config.account_pool.accounts else None
    headers_by_model: dict[str, dict[str, str]] = {}
    account_by_model: dict[str, str] = {}
    if pool is not None:
        for model in models:
            account = pool.get_next()
            if account is None:
                continue
            account_by_model[model] = account.name
            token = pool.resolve_token(account)
            if token:
                headers_by_model[model] = injector.build_headers(token=token)
        if headers_by_model:
            log.info(
                "Probe with account auth",
                extra={"accounts": sorted(set(account_by_model.values()))},
            )

    log.info("Probing %d models", len(models), extra={"models": models})
    results = await prober.probe_all(models, headers, headers_by_model or None)

    for r in results:
        name = account_by_model.get(r.model)
        if name:
            pool.mark_result(name, success=r.status == "available", latency_ms=r.latency_ms)  # type: ignore[union-attr]

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False))
    else:
        status_icons = {"available": "+", "rate_limited": "!", "error": "x", "unknown": "?"}
        for r in results:
            icon = status_icons.get(r.status, "?")
            print(f"  [{icon}] {r.model}: {r.status} ({r.latency_ms:.0f}ms)")
            if r.retry_after:
                print(f"      retry_after: {r.retry_after}s")
            if r.error:
                print(f"      error: {r.error}")

    any_limited = any(r.status == "rate_limited" for r in results)
    return 1 if any_limited else 0


async def cmd_headers(config: Config, args: argparse.Namespace) -> int:
    version = get_opencode_version()
    injector = HeaderInjector(config.headers, version)

    if args.export:
        print(injector.to_env_export(args.model))
    else:
        headers = injector.build_headers(args.model)
        print(json.dumps(headers, indent=2, ensure_ascii=False))

    return 0


async def cmd_rotate(config: Config, args: argparse.Namespace) -> int:
    log = logging.getLogger("cmd.rotate")

    # Override strategy if provided via CLI
    config.account_pool.strategy = args.strategy
    pool = AccountPool(config.account_pool)

    if not pool.accounts:
        log.warning("No accounts configured in account_pool")
        if args.json:
            print(json.dumps({"error": "no accounts configured"}, indent=2))
        else:
            print("No accounts configured. Add accounts to [account_pool] in config.toml")
        return 1

    next_account = pool.get_next()
    if next_account is None:
        log.warning("Account pool returned no account despite non-empty pool")
        return 1

    token = pool.resolve_token(next_account)
    log.info(
        "Rotating to account",
        extra={
            "account": next_account.name,
            "strategy": args.strategy,
            "dry_run": args.dry_run,
            "auth_token": token is not None,
        },
    )

    if args.json:
        print(
            json.dumps(
                {
                    "rotated_to": next_account.name,
                    "strategy": args.strategy,
                    "accounts": [a.name for a in pool.accounts],
                    "auth_token_resolved": token is not None,
                    "dry_run": bool(args.dry_run),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(f"Rotated to account: {next_account.name}")
        print(f"Strategy: {args.strategy}")
        print(f"Available accounts: {', '.join(a.name for a in pool.accounts)}")
        print(f"Auth token resolved: {'yes' if token else 'no'}")
        if args.dry_run:
            print("(dry run) Preview only - selection shown, nothing was changed")

    return 0


async def cmd_check(config: Config, args: argparse.Namespace) -> int:
    log = logging.getLogger("cmd.check")
    log.info("Running health check")

    version = get_opencode_version()
    health = {
        "timestamp": _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z"),
        "config_valid": True,
        "opencode_version": version,
        "config_paths": {
            "config_dirs": [str(p) for p in get_opencode_config_dirs()],
            "cache_dirs": [str(p) for p in config.get_cache_dirs()],
            "state_files": [str(p) for p in config.get_state_files()],
            "auth_files": [str(p) for p in get_opencode_auth_files()],
        },
        "account_pool": {
            "configured_accounts": len(config.account_pool.accounts),
            "strategy": config.account_pool.strategy,
            "accounts": [
                {"name": a.name, "auth_path": a.auth_path, "env_var": a.env_var}
                for a in [Account(**acc) for acc in config.account_pool.accounts]
            ],
        },
        "daemon": {
            "interval_seconds": config.daemon.interval_seconds,
            "models": config.daemon.models,
            "auto_cleanup_on_429": config.daemon.auto_cleanup_on_429,
        },
    }

    state = load_daemon_state()
    if state is not None:
        # Merge runtime daemon status over the configured daemon defaults
        merged = dict(cast("dict[str, Any]", health["daemon"]))
        merged.update(state)
        health["daemon"] = merged
        # Surface per-account health tracked by the daemon, if any (moved out
        # of the daemon section to account_pool.health)
        pool_health = merged.pop("pool_health", None)
        if pool_health:
            cast("dict[str, Any]", health["account_pool"])["health"] = pool_health

    if args.json:
        print(json.dumps(health, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(health, indent=2, ensure_ascii=False))

    return 0


# =============================================================================
# Phase 3: Daemon
# =============================================================================


def _now_iso() -> str:
    """Current UTC timestamp in ISO-8601 Z format"""
    return _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")


def _make_signal_bridge(
    loop: asyncio.AbstractEventLoop, callback: Callable[[], None]
) -> Callable[[int, Any], None]:
    """Bridge a stdlib signal handler into the running asyncio loop"""

    def _bridge(signum: int, frame: Any) -> None:
        loop.call_soon_threadsafe(callback)

    return _bridge


@dataclass
class DaemonStatus:
    """Runtime state snapshot for the daemon"""

    running: bool = False
    started_at: float = 0.0
    last_probe: str = ""
    next_probe: str = ""
    last_cleanup: str = ""
    total_cycles: int = 0
    total_cleanups: int = 0
    model_results: dict[str, ProbeResult] = field(default_factory=dict)
    pool_health: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        import time

        uptime = 0
        if self.running and self.started_at:
            uptime = int(time.monotonic() - self.started_at)
        return {
            "running": self.running,
            "uptime_seconds": uptime,
            "last_probe": self.last_probe,
            "next_probe": self.next_probe,
            "last_cleanup": self.last_cleanup,
            "total_cycles": self.total_cycles,
            "total_cleanups": self.total_cleanups,
            "models": {name: r.to_dict() for name, r in sorted(self.model_results.items())},
            "pool_health": self.pool_health,
        }


class DaemonLockError(RuntimeError):
    """Raised when another live daemon instance already holds the lock"""

    def __init__(self, pid: int, lock_path: Path):
        super().__init__(
            f"another daemon instance appears to be running (pid {pid}, lock {lock_path})"
        )
        self.pid = pid
        self.lock_path = lock_path


def get_daemon_lock_path() -> Path:
    """Path to the daemon's single-instance lock file"""
    return Path(user_state_dir("opencode-rate-limiter")) / "daemon.lock"


def _pid_alive(pid: int) -> bool:
    """Check whether a process id is alive (cross-platform, never signals)"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) would TERMINATE the process on Windows, so query via ctypes
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # noqa: N806 - Win32 constant
        STILL_ACTIVE = 259  # noqa: N806 - Win32 constant
        # getattr (not attribute access): ctypes.windll only exists in the
        # win32 stubs, so a plain access fails mypy on Linux CI while a
        # suppression comment is flagged as unused on Windows.
        kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    import errno

    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    return True


class RateLimiterDaemon:
    """Periodic probe daemon with auto-cleanup, account rotation and signal control"""

    def __init__(
        self,
        config: Config,
        config_path: Path | None = None,
        interval_override: int | None = None,
        models_override: list[str] | None = None,
        state_path: Path | None = None,
        lock_path: Path | None = None,
    ):
        self.config = config
        self.config_path = config_path
        self._interval_override = interval_override
        self._models_override = models_override
        self._state_path = state_path
        self._lock_path = lock_path
        self._lock_file: Path | None = None
        self.log = logging.getLogger("daemon")
        self.status = DaemonStatus()
        self._running = False
        self._stop_event = asyncio.Event()
        self._probe_event = asyncio.Event()
        self._error_streak = 0
        self._prev_handlers: dict[int, Any] = {}
        self._signal_fallback_sigs: list[int] = []
        self._rebuild()

    def _effective_interval(self) -> int:
        return (
            self._interval_override
            if self._interval_override is not None
            else self.config.daemon.interval_seconds
        )

    def _effective_models(self) -> list[str]:
        if self._models_override:
            return self._models_override
        return self.config.daemon.models

    def _rebuild(self) -> None:
        """(Re)build runtime components from current config"""
        version = get_opencode_version()
        self.injector = HeaderInjector(self.config.headers, version)
        self.headers = self.injector.build_headers()
        self.prober = ModelProber(self.config.daemon.probe_timeout_seconds, self.config.prober)
        self.cleanup = CleanupManager(self.config.cleanup)
        self.pool = (
            AccountPool(self.config.account_pool) if self.config.account_pool.accounts else None
        )

    async def run(self) -> None:
        import time

        self._acquire_lock()
        self._install_signal_handlers()
        self._running = True
        self.status.running = True
        self.status.started_at = time.monotonic()
        interval = self._effective_interval()
        self.log.info(
            "Daemon started",
            extra={"interval": interval, "models": len(self._effective_models())},
        )
        try:
            while self._running:
                await self._probe_cycle()
                if not self._running:
                    break
                wait_seconds = interval * self._backoff_multiplier()
                if wait_seconds > interval:
                    self.log.warning(
                        "All probes errored; backing off",
                        extra={"wait_seconds": wait_seconds, "error_streak": self._error_streak},
                    )
                await self._wait(wait_seconds)
        finally:
            self.status.running = False
            self._running = False
            self._restore_signal_handlers()
            self._persist_state()
            self._release_lock()
            self.log.info(
                "Daemon stopped",
                extra={
                    "cycles": self.status.total_cycles,
                    "cleanups": self.status.total_cleanups,
                },
            )

    def _acquire_lock(self) -> None:
        """Create the single-instance lock file, taking over stale locks"""
        lock_path = self._lock_path or get_daemon_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = self._read_lock_pid(lock_path)
            if existing is not None and _pid_alive(existing):
                raise DaemonLockError(existing, lock_path) from None
            # Stale lock from a dead process - take over
            self.log.warning("Removing stale daemon lock (pid %s)", existing)
            with contextlib.suppress(OSError):
                lock_path.unlink()
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        self._lock_file = lock_path

    @staticmethod
    def _read_lock_pid(lock_path: Path) -> int | None:
        try:
            return int(lock_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _release_lock(self) -> None:
        """Remove the lock file only if we still own it"""
        if self._lock_file is None:
            return
        if self._read_lock_pid(self._lock_file) == os.getpid():
            with contextlib.suppress(OSError):
                self._lock_file.unlink()
        self._lock_file = None

    async def _wait(self, interval: int) -> None:
        """Wait for the next probe, interrupted by stop or forced-probe signals"""
        stop_task = asyncio.create_task(self._stop_event.wait())
        probe_task = asyncio.create_task(self._probe_event.wait())
        try:
            done, _ = await asyncio.wait(
                {stop_task, probe_task}, timeout=interval, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (stop_task, probe_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stop_task, probe_task, return_exceptions=True)
        if stop_task in done:
            self._running = False
        if self._probe_event.is_set():
            self._probe_event.clear()

    async def _probe_cycle(self) -> None:
        models = self._effective_models()
        self.status.total_cycles += 1
        self.status.last_probe = _now_iso()
        self.log.info("Starting probe cycle", extra={"models": len(models)})

        # Per-model account rotation: pick an account per model and inject its
        # auth token into that probe's headers when resolvable.
        headers_by_model: dict[str, dict[str, str]] = {}
        account_by_model: dict[str, str] = {}
        if self.pool is not None:
            for model in models:
                account = self.pool.get_next()
                if account is None:
                    continue
                account_by_model[model] = account.name
                token = self.pool.resolve_token(account)
                if token:
                    headers_by_model[model] = self.injector.build_headers(token=token)

        results = await self.prober.probe_all(models, self.headers, headers_by_model or None)

        for r in results:
            self.status.model_results[r.model] = r
            self.log.info(
                "Probe completed",
                extra={
                    "model": r.model,
                    "status": r.status,
                    "http_status": r.http_status,
                    "latency_ms": round(r.latency_ms, 1),
                },
            )
            if r.retry_after:
                self.log.warning(
                    "Rate limit window", extra={"model": r.model, "retry_after": r.retry_after}
                )
            # Feed results back into health tracking; rate_limited is marked as
            # a failure inside _handle_rate_limited (which also rotates).
            if r.status != "rate_limited":
                name = account_by_model.get(r.model)
                if name and self.pool is not None:
                    self.pool.mark_result(
                        name, success=r.status == "available", latency_ms=r.latency_ms
                    )

        for r in results:
            if r.status == "rate_limited":
                await self._handle_rate_limited(r, account_by_model.get(r.model))

        # Backoff: every probe in the cycle errored (network/endpoint trouble)
        if results and all(r.status == "error" for r in results):
            self._error_streak += 1
        else:
            self._error_streak = 0

        # Snapshot account health for the persisted state file
        if self.pool is not None:
            self.status.pool_health = {
                name: {
                    "success": h.success_count,
                    "total": h.total_count,
                    "consecutive_failures": h.consecutive_failures,
                    "avg_latency_ms": round(h.avg_latency_ms, 1),
                    "score": round(h.calculate_score(), 3),
                }
                for name, h in sorted(self.pool.health.items())
            }

        next_ts = _dt.datetime.now(_dt.UTC) + _dt.timedelta(
            seconds=self._effective_interval() * self._backoff_multiplier()
        )
        self.status.next_probe = next_ts.isoformat().replace("+00:00", "Z")
        self._persist_state()

    def _backoff_multiplier(self) -> int:
        """Exponential wait multiplier after consecutive all-error cycles (cap 8x)"""
        return 1 << min(self._error_streak, 3)

    def _persist_state(self) -> None:
        """Write the current status snapshot to the state file"""
        data = self.status.to_dict()
        data["pid"] = os.getpid()
        data["updated_at"] = _now_iso()
        write_daemon_state(data, self._state_path)

    async def _handle_rate_limited(
        self, result: ProbeResult, account_name: str | None = None
    ) -> None:
        self.log.warning(
            "Rate limited detected",
            extra={
                "model": result.model,
                "retry_after": result.retry_after,
                "estimated_reset": result.estimated_reset,
            },
        )

        # Mark the account that actually served this model as failing, then
        # rotate away from it (when pool has 2+)
        if self.pool is not None:
            current = self.pool.get_current()
            name = account_name or (current.name if current is not None else None)
            if name is not None:
                self.pool.mark_result(name, success=False, latency_ms=result.latency_ms)
            if len(self.pool.accounts) > 1:
                next_account = self.pool.get_next()
                if next_account is not None:
                    self.log.info(
                        "Account rotated",
                        extra={
                            "from": name,
                            "to": next_account.name,
                            "strategy": self.config.account_pool.strategy,
                        },
                    )

        if self.config.daemon.auto_cleanup_on_429:
            await asyncio.to_thread(self.cleanup.full_cleanup)
            self.status.total_cleanups += 1
            self.status.last_cleanup = _now_iso()
            self.log.info("Auto cleanup triggered", extra={"trigger": "429", "model": result.model})

    def _install_signal_handlers(self) -> None:
        import signal

        loop = asyncio.get_running_loop()
        mapping: dict[str, Callable[[], None]] = {
            "SIGTERM": self._request_stop,
            "SIGINT": self._request_stop,
            "SIGHUP": self._reload_config,
            "SIGUSR1": self._request_probe,
            "SIGUSR2": self._request_status,
        }
        for name, callback in mapping.items():
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, callback)
            except NotImplementedError:
                previous = signal.getsignal(sig)
                self._prev_handlers[int(sig)] = previous
                self._signal_fallback_sigs.append(int(sig))
                signal.signal(sig, _make_signal_bridge(loop, callback))
            except RuntimeError:
                self.log.debug("Signal handler install skipped for %s", name)

    def _restore_signal_handlers(self) -> None:
        import signal

        for sig_int in self._signal_fallback_sigs:
            previous = self._prev_handlers.get(sig_int)
            if previous is None:
                continue
            with contextlib.suppress(OSError, ValueError):
                signal.signal(sig_int, previous)
        self._signal_fallback_sigs = []
        self._prev_handlers = {}

    def _request_stop(self) -> None:
        self.log.info("Stopping daemon (graceful)")
        self._running = False
        self._stop_event.set()

    def _request_probe(self) -> None:
        self._probe_event.set()

    def _request_status(self) -> None:
        self.log.info("Daemon status", extra=self.status.to_dict())

    def _reload_config(self) -> None:
        self.log.info("Reloading config (SIGHUP)")
        try:
            new_config = Config.load(self.config_path)
        except Exception as e:
            self.log.error("Config reload failed: %s", e)
            return
        # CLI overrides survive the reload
        new_config.daemon.interval_seconds = self._effective_interval()
        if self._models_override:
            new_config.daemon.models = list(self._models_override)
        self.config = new_config
        self._rebuild()
        self.log.info(
            "Config reloaded",
            extra={"interval": self._effective_interval(), "models": len(self._effective_models())},
        )


async def cmd_daemon(config: Config, args: argparse.Namespace) -> int:
    log = logging.getLogger("cmd.daemon")

    if args.dry_run:
        log.info(
            "DRY RUN: Would start daemon",
            extra={"interval": args.interval, "models": args.models},
        )
        return 0

    interval_override = args.interval
    if interval_override is not None and interval_override < 5:
        log.error("interval must be >= 5 seconds, got %d", interval_override)
        return 2

    models_override: list[str] | None = None
    if args.models:
        models_override = [m.strip() for m in args.models.split(",") if m.strip()]
        if not models_override:
            log.error("--models produced an empty model list")
            return 2

    daemon = RateLimiterDaemon(
        config,
        config_path=args.config,
        interval_override=interval_override,
        models_override=models_override,
    )
    try:
        await daemon.run()
    except DaemonLockError as e:
        log.error("Not starting daemon: %s", e)
        return 1
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    return 0


# =============================================================================
# Phase 4: CLI 界面整合 - 状态持久化与服务文件生成
# =============================================================================


def get_daemon_state_path() -> Path:
    """Path to the daemon's persisted state file"""
    return Path(user_state_dir("opencode-rate-limiter")) / "daemon.json"


def write_daemon_state(data: dict[str, Any], path: Path | None = None) -> None:
    """Atomically persist daemon state to disk"""
    target = path or get_daemon_state_path()
    log = logging.getLogger("daemon")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(target)
    except OSError as e:
        log.debug("Failed to write daemon state: %s", e)


def load_daemon_state() -> dict[str, Any] | None:
    """Read the daemon's persisted state file, if present"""
    path = get_daemon_state_path()
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return cast("dict[str, Any]", json.load(f))
    except (json.JSONDecodeError, OSError) as e:
        logging.getLogger("main").debug("Failed to read daemon state: %s", e)
        return None


def _default_config_path_str() -> str:
    return str(Path(user_config_dir("opencode-rate-limiter")) / "config.toml")


def _resolve_binary() -> str:
    import shutil

    return shutil.which("opencode-rate-limiter") or "opencode-rate-limiter"


def generate_systemd_unit() -> str:
    """Generate a systemd user unit file for the daemon"""
    return f"""\
[Unit]
Description=OpenCode Rate Limiter Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
ExecStart={_resolve_binary()} daemon
Restart=on-failure
RestartSec=10
Environment=OPENCODE_RATE_LIMITER_CONFIG={_default_config_path_str()}
# 可选: 限制资源
MemoryMax=100M
CPUQuota=10%

[Install]
WantedBy=default.target
"""


def generate_launchd_plist() -> str:
    """Generate a macOS launchd plist for the daemon"""
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.opencode.ratelimiter</string>
    <key>ProgramArguments</key>
    <array>
        <string>{_resolve_binary()}</string>
        <string>daemon</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/opencode-rate-limiter.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/opencode-rate-limiter.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>OPENCODE_RATE_LIMITER_CONFIG</key>
        <string>{_default_config_path_str()}</string>
    </dict>
</dict>
</plist>
"""


def generate_task_xml() -> str:
    """Generate a Windows Task Scheduler XML for the daemon"""
    return f"""\
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>OpenCode Rate Limiter Daemon</Description>
    <Author>opencode-rate-limiter</Author>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
  </Settings>
  <Actions>
    <Exec>
      <Command>{_resolve_binary()}</Command>
      <Arguments>daemon</Arguments>
      <WorkingDirectory>%USERPROFILE%</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


async def cmd_generate_systemd(config: Config, args: argparse.Namespace) -> int:
    print(generate_systemd_unit())
    return 0


async def cmd_generate_launchd(config: Config, args: argparse.Namespace) -> int:
    print(generate_launchd_plist())
    return 0


async def cmd_generate_task(config: Config, args: argparse.Namespace) -> int:
    print(generate_task_xml())
    return 0


async def cmd_generate_config(config: Config, args: argparse.Namespace) -> int:
    log = logging.getLogger("cmd.generate-config")
    target = args.config or Path(_default_config_path_str())

    if target.exists() and not args.force:
        log.error("Config file already exists: %s (use --force to overwrite)", target)
        return 1

    try:
        # Write the built-in defaults as a starting template for the user
        Config().save(target)
    except (RuntimeError, OSError) as e:
        log.error("Failed to write config: %s", e)
        return 1

    if args.json:
        print(json.dumps({"path": str(target), "written": True}, indent=2))
    else:
        print(f"Config written to {target}")
        print("Edit [account_pool] to add accounts before using `rotate` / `daemon`.")
    return 0


# =============================================================================
# Phase 5: Shell 补全生成
# =============================================================================

_STRUCTURED_COMMANDS = frozenset(
    {
        "check",
        "probe",
        "headers",
        "generate-systemd",
        "generate-launchd",
        "generate-task",
        "generate-config",
        "completions",
    }
)

_GLOBAL_FLAGS = {
    "--config",
    "--json",
    "--verbose",
    "--quiet",
    "--dry-run",
    "--version",
    "--help",
    "-h",
}


def should_print_banner(command: str, json_output: bool) -> bool:
    """Return True when the human-facing banner should be printed for a command."""
    return not json_output and command not in _STRUCTURED_COMMANDS


def _completion_payload() -> dict[str, Any]:
    """Derive completion data from the live CLI parser (single source of truth)."""
    parser = build_parser()
    commands: list[dict[str, str]] = []
    global_options: list[str] = []
    sub_options: dict[str, list[str]] = {}
    choice_opt: dict[str, list[str]] = {}
    positional_choices: dict[str, list[str]] = {}

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name in sorted(action.choices):
                sub = action.choices[name]
                commands.append({"name": name, "help": sub.description or ""})
                opts: list[str] = []
                for a in sub._actions:
                    if not a.option_strings:
                        if a.dest == "model":
                            positional_choices[name] = [*FREE_MODELS, "all"]
                        continue
                    primary = a.option_strings[0]
                    if primary in _GLOBAL_FLAGS:
                        continue
                    opts.append(primary)
                    if a.choices:
                        choice_opt[primary] = [str(c) for c in a.choices]
                sub_options[name] = opts
        else:
            for flag in action.option_strings:
                if flag not in global_options:
                    global_options.append(flag)

    return {
        "prog": parser.prog,
        "commands": commands,
        "global_options": global_options,
        "sub_options": sub_options,
        "choice_opt": choice_opt,
        "positional_choices": positional_choices,
    }


def _bash_completion(payload: dict[str, Any]) -> str:
    prog = payload["prog"]
    func = prog.replace("-", "_")
    commands = " ".join(c["name"] for c in payload["commands"])
    globals_flags = " ".join(payload["global_options"])
    opts_word = " ".join([commands, globals_flags])

    case_lines = ['        --config) COMPREPLY=( $(compgen -f -- "${cur}") ); return 0 ;;']
    for flag in sorted(payload["choice_opt"]):
        choices = " ".join(payload["choice_opt"][flag])
        case_lines.append(
            '        %s) COMPREPLY=( $(compgen -W "%s" -- "${cur}") ); return 0 ;;'
            % (flag, choices)
        )
    for name in sorted(payload["sub_options"]):
        opts = payload["sub_options"][name]
        if opts:
            case_lines.append(
                '        %s) COMPREPLY=( $(compgen -W "%s" -- "${cur}") ); return 0 ;;'
                % (name, " ".join(opts))
            )
    models = " ".join(payload["positional_choices"].get("probe", []))
    if models:
        case_lines.append(
            '        probe) COMPREPLY=( $(compgen -W "%s" -- "${cur}") ); return 0 ;;' % models
        )

    return """# %(prog)s bash completion
_%(func)s() {
    local cur prev
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    case "${prev}" in
%(case)s
    esac

    COMPREPLY=( $(compgen -W "%(opts)s" -- "${cur}") )
    return 0
}
complete -F _%(func)s %(prog)s
""" % {
        "prog": prog,
        "func": func,
        "case": "\n".join(case_lines),
        "opts": opts_word,
    }


def _zsh_completion(payload: dict[str, Any]) -> str:
    prog = payload["prog"]
    func = prog.replace("-", "_")

    commands_block = "\n".join(
        "        '%s:%s'" % (c["name"], c["help"]) for c in payload["commands"]
    )
    options_block = "\n".join("        '%s'" % f for f in payload["global_options"])
    models = payload["positional_choices"].get("probe", [])
    models_block = "\n".join("        '%s'" % m for m in models)
    strategies = payload["choice_opt"].get("--strategy", [])
    strategies_block = "\n".join("        '%s'" % s for s in strategies)

    subargs: list[str] = []
    if "--strategy" in payload["choice_opt"]:
        subargs.append("        '--strategy: :($strategies)'")
    for name in sorted(payload["sub_options"]):
        opts = payload["sub_options"][name]
        if opts:
            subargs.append("        '%s: :(%s)'" % (name, " ".join(opts)))
    if models:
        subargs.append("        'probe: :($probe_models)'")

    return """# %(prog)s zsh completion
#compdef %(prog)s

_%(func)s() {
    local -a commands options probe_models strategies

    commands=(
%(commands)s
    )

    options=(
%(options)s
    )

    probe_models=(
%(models)s
    )

    strategies=(
%(strategies)s
    )

    _arguments -C \\
        ${options} \\
        '(-)'{${commands}} \\
%(subargs)s
}

_%(func)s "$@"
""" % {
        "prog": prog,
        "func": func,
        "commands": commands_block,
        "options": options_block,
        "models": models_block,
        "strategies": strategies_block,
        "subargs": "\n".join(subargs),
    }


def _fish_completion(payload: dict[str, Any]) -> str:
    prog = payload["prog"]
    func = prog.replace("-", "_")

    commands_block = "\n".join("        %s" % c["name"] for c in payload["commands"])
    models = payload["positional_choices"].get("probe", [])
    models_block = "\n".join("        %s" % m for m in models)
    strategies_line = " ".join(payload["choice_opt"].get("--strategy", []))

    extra: list[str] = []
    for name in sorted(payload["sub_options"]):
        opts = [o for o in payload["sub_options"][name] if o not in payload["choice_opt"]]
        if not opts:
            continue
        extra.append('complete -c %s -f -n "__fish_seen_subcommand_from %s" \\' % (prog, name))
        for opt in opts:
            extra.append("    -l %s" % opt.lstrip("-"))
    if "--strategy" in payload["choice_opt"]:
        extra.append(
            'complete -c %s -f -n "__fish_seen_subcommand_from rotate" -l strategy '
            '-a "(__fish_%s_strategies)"' % (prog, func)
        )

    return """# %(prog)s fish completion
function __fish_%(func)s_commands
    set -l commands \\
%(commands)s
    for cmd in $commands
        echo $cmd
    end
end

function __fish_%(func)s_probe_models
    set -l models \\
%(models)s
    for model in $models
        echo $model
    end
end

function __fish_%(func)s_strategies
    set -l strategies %(strategies)s
    for s in $strategies
        echo $s
    end
end

complete -c %(prog)s -f -n "__fish_use_subcommand" \\
    -a "(__fish_%(func)s_commands)"

complete -c %(prog)s -f -n "__fish_seen_subcommand_from probe" \\
    -a "(__fish_%(func)s_probe_models)"

%(extra)s

complete -c %(prog)s -f \\
    -l config \\
    -l json \\
    -l verbose \\
    -l quiet \\
    -l dry-run \\
    -l version
""" % {
        "prog": prog,
        "func": func,
        "commands": commands_block,
        "models": models_block,
        "strategies": strategies_line,
        "extra": "\n".join(extra),
    }


def generate_completions(shell: str) -> str:
    """Generate a shell completion script (bash / zsh / fish) for the CLI."""
    payload = _completion_payload()
    if shell == "bash":
        return _bash_completion(payload)
    if shell == "zsh":
        return _zsh_completion(payload)
    if shell == "fish":
        return _fish_completion(payload)
    raise ValueError(f"Unsupported shell: {shell}")


async def cmd_completions(config: Config, args: argparse.Namespace) -> int:
    print(generate_completions(args.shell))
    return 0


COMMAND_HANDLERS = {
    "quick": cmd_quick,
    "deep": cmd_deep,
    "probe": cmd_probe,
    "headers": cmd_headers,
    "rotate": cmd_rotate,
    "check": cmd_check,
    "daemon": cmd_daemon,
    "generate-systemd": cmd_generate_systemd,
    "generate-launchd": cmd_generate_launchd,
    "generate-task": cmd_generate_task,
    "generate-config": cmd_generate_config,
    "completions": cmd_completions,
}


# =============================================================================
# Main Entry Point
# =============================================================================


def print_banner() -> None:
    print("=" * 60)
    print(f"      OpenCode 免费模型 Rate-Limit 缓解工具 v{__version__}")
    print("=" * 60)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    setup_logging(level_from_args(args), args.json)

    if args.dry_run:
        logging.getLogger("main").info("DRY RUN MODE - no changes will be made")

    try:
        config = Config.load(args.config)
    except Exception as e:
        logging.getLogger("main").error("Failed to load config: %s", e)
        return 2

    if should_print_banner(args.command, args.json):
        print_banner()

    handler = COMMAND_HANDLERS.get(args.command)
    if handler:
        import asyncio

        try:
            return asyncio.run(handler(config, args))
        except KeyboardInterrupt:
            logging.getLogger("main").info("Interrupted by user")
            return 130
        except Exception as e:
            logging.getLogger("main").error("Command failed: %s", e, exc_info=args.verbose >= 1)
            return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
