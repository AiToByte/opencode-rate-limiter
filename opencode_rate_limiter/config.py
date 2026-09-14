"""Configuration model: TOML + env loading, merging, validation, path expansion."""

from __future__ import annotations

import copy
import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

try:
    import tomli_w
except ImportError:
    tomli_w: Any = None  # type: ignore[no-redef]

from platformdirs import user_config_dir

from .paths import (
    _dedupe,
    get_opencode_native_cache_dirs,
    get_opencode_native_state_files,
)

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
    # Probes consume the same per-IP daily free quota as real usage, so the
    # default cadence is deliberately conservative (see MANUAL, Zen limits).
    interval_seconds: int = 900
    models: list[str] = field(default_factory=lambda: list(FREE_MODELS))
    probe_timeout_seconds: float = 10.0
    auto_cleanup_on_429: bool = True
    history_size: int = 20
    event_history_size: int = 50
    respect_cooldown: bool = True
    notify_webhook: str | None = None
    notify_command: str | None = None
    # Hard cap on probe requests per UTC day (0 = unlimited). The server
    # counts every probe against the IP's free daily quota.
    daily_probe_budget: int = 200
    # Key-dimension cooldown for accounts that hit RateLimitError (seconds).
    key_cooldown_seconds: int = 60

    def validate(self) -> None:
        if self.interval_seconds < 5:
            raise ValueError(f"interval_seconds must be >= 5, got {self.interval_seconds}")
        if self.probe_timeout_seconds <= 0:
            raise ValueError(f"probe_timeout_seconds must be > 0, got {self.probe_timeout_seconds}")
        if not self.models:
            raise ValueError("models list cannot be empty")
        if self.history_size < 1:
            raise ValueError(f"history_size must be >= 1, got {self.history_size}")
        if self.event_history_size < 1:
            raise ValueError(f"event_history_size must be >= 1, got {self.event_history_size}")
        if self.daily_probe_budget < 0:
            raise ValueError(f"daily_probe_budget must be >= 0, got {self.daily_probe_budget}")
        if self.key_cooldown_seconds < 0:
            raise ValueError(f"key_cooldown_seconds must be >= 0, got {self.key_cooldown_seconds}")
        if self.notify_webhook and not self.notify_webhook.startswith(("http://", "https://")):
            raise ValueError(f"notify_webhook must be an http(s) URL, got {self.notify_webhook}")


@dataclass
class AccountPoolConfig:
    accounts: list[dict[str, Any]] = field(default_factory=list)
    strategy: Literal["round_robin", "least_used", "health"] = "health"
    health_window: int = 100
    score_weights: dict[str, float] = field(
        default_factory=lambda: {"success": 0.5, "latency": 0.3, "recency": 0.2}
    )

    _WEIGHT_KEYS = ("success", "latency", "recency")

    def validate(self) -> None:
        valid_strategies = {"round_robin", "least_used", "health"}
        if self.strategy not in valid_strategies:
            raise ValueError(f"strategy must be one of {valid_strategies}, got {self.strategy}")
        if self.health_window < 1:
            raise ValueError(f"health_window must be >= 1, got {self.health_window}")
        weights = self.score_weights
        missing = [k for k in self._WEIGHT_KEYS if k not in weights]
        if missing:
            raise ValueError(f"score_weights missing keys: {missing}")
        unknown = [k for k in weights if k not in self._WEIGHT_KEYS]
        if unknown:
            raise ValueError(f"score_weights has unknown keys: {unknown}")
        if not all(0.0 <= float(v) <= 1.0 for v in weights.values()):
            raise ValueError("score_weights values must be within [0.0, 1.0]")
        if abs(sum(float(v) for v in weights.values()) - 1.0) > 0.001:
            raise ValueError("score_weights must sum to 1.0")
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
            if "kind" in acc and acc["kind"] not in ("oauth", "api"):
                raise ValueError(f"accounts[{i}].kind must be 'oauth' or 'api'")
        names = [str(acc.get("name")) for acc in self.accounts if isinstance(acc, dict)]
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"duplicate account names: {dupes}")


@dataclass
class ProberConfig:
    """Probe request customization (endpoint, payload, extra headers, proxy)"""

    endpoint: str = "https://opencode.ai/zen/v1/chat/completions"
    ping_message: str = "ping"
    max_tokens: int = 1
    extra_headers: dict[str, str] = field(default_factory=dict)
    proxy: str | None = None
    http2: bool = False
    connection_pool_size: int = 8
    # Immediate retries for transient network failures only (connect
    # errors / timeouts). HTTP statuses — including 429 — are never
    # retried: 429 burns quota and is handled by daemon cooldowns instead.
    max_retries: int = 0
    # Explicit x-opencode-session header value. When unset, each ModelProber
    # generates its own random session id (the gateway rejects session-less
    # requests with MissingSessionID).
    session_id: str | None = None

    def validate(self) -> None:
        if not self.endpoint.startswith(("http://", "https://")):
            raise ValueError(f"prober.endpoint must be an http(s) URL, got {self.endpoint}")
        if self.max_tokens < 1:
            raise ValueError(f"prober.max_tokens must be >= 1, got {self.max_tokens}")
        if self.connection_pool_size < 1:
            raise ValueError(
                f"prober.connection_pool_size must be >= 1, got {self.connection_pool_size}"
            )
        if self.max_retries < 0:
            raise ValueError(f"prober.max_retries must be >= 0, got {self.max_retries}")
        if self.session_id is not None and not self.session_id.strip():
            raise ValueError("prober.session_id must be non-empty when set")


@dataclass
class HeadersConfig:
    user_agent: str = "opencode/{version}"
    x_opencode_client: str = "opencode-cli"
    x_opencode_version: str = "{version}"

    def validate(self) -> None:
        for field_name in ("user_agent", "x_opencode_client", "x_opencode_version"):
            value = getattr(self, field_name)
            try:
                value.format(version="", model="")
            except (KeyError, IndexError, ValueError) as e:
                raise ValueError(f"headers.{field_name} has an unsupported placeholder: {e}") from e


@dataclass
class CleanupConfig:
    cache_dirs: list[str] = field(default_factory=list)
    state_files: list[str] = field(default_factory=list)
    preserve_config: bool = True

    def validate(self) -> None:
        if not self.preserve_config:
            raise ValueError("preserve_config must be true (protects user config.json)")


def _match_section(sections: dict[str, Any], name: Any) -> Any | None:
    """Resolve a section name: exact match first, case-insensitive fallback."""
    if not isinstance(name, str):
        return None
    if name in sections:
        return sections[name]
    lowered = name.lower()
    for section_name, target in sections.items():
        if section_name.lower() == lowered:
            return target
    return None


def _match_field(target: object, key: Any) -> str | None:
    """Resolve a field name against a config object, preferring exact case."""
    if not isinstance(key, str):
        return None
    if hasattr(target, key):
        return key
    lowered = key.lower()
    for attr in vars(target):
        if attr.lower() == lowered:
            return attr
    return None


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
        # JSON first: covers quoted strings, numbers, lists and objects
        # without mistaking URLs or paths containing commas for lists.
        stripped = value.strip()
        if stripped[:1] in ("[", "{", '"'):
            try:
                return json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                pass
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
        # List (comma-separated, only when it does not look like a URL/path)
        if "," in value and "://" not in value and "/" not in value:
            return [v.strip() for v in value.split(",")]
        # String
        return value

    @staticmethod
    def _merge_value(current_value: Any, new_value: Any) -> Any:
        """Merge a config value: nested tables merge recursively, everything
        else coerces.

        Recursive merging lets partial tables (e.g. only two of the three
        score_weights) override defaults instead of replacing them; a full
        replacement is still possible by specifying every key.
        """
        if isinstance(current_value, dict) and isinstance(new_value, dict):
            merged = dict(current_value)
            for key, value in new_value.items():
                if key in merged:
                    merged[key] = Config._merge_value(merged[key], value)
                else:
                    merged[key] = value
            return merged
        return Config._coerce_value(current_value, new_value)

    @staticmethod
    def _coerce_value(current_value: Any, new_value: Any) -> Any:
        """Coerce new_value to match the type of current_value"""
        if isinstance(current_value, bool):
            if isinstance(new_value, str):
                return new_value.lower() in ("true", "1", "yes")
            return bool(new_value)
        if isinstance(current_value, int):
            return int(new_value)
        if isinstance(current_value, float):
            return float(new_value)
        return new_value

    @classmethod
    def _merge(cls, base: Config, override: dict[str, Any]) -> Config:
        """Merge override dict into base config (base is never mutated)"""
        result = cls(
            daemon=copy.deepcopy(base.daemon),
            account_pool=copy.deepcopy(base.account_pool),
            prober=copy.deepcopy(base.prober),
            headers=copy.deepcopy(base.headers),
            cleanup=copy.deepcopy(base.cleanup),
        )

        sections: dict[str, Any] = {
            "daemon": result.daemon,
            "account_pool": result.account_pool,
            "prober": result.prober,
            "headers": result.headers,
            "cleanup": result.cleanup,
        }
        for section_name, values in override.items():
            target = _match_section(sections, section_name)
            if target is None or not isinstance(values, dict):
                continue
            for key, value in values.items():
                attr = _match_field(target, key)
                if attr is not None:
                    current = getattr(target, attr)
                    setattr(target, attr, cls._merge_value(current, value))

        return result

    def validate(self) -> None:
        self.daemon.validate()
        self.account_pool.validate()
        self.prober.validate()
        self.headers.validate()
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
        return _dedupe(dirs)

    def get_state_files(self) -> list[Path]:
        """Get expanded state files (includes OpenCode native paths)"""
        files = self._expanded_state_files.copy()
        files.extend(get_opencode_native_state_files())
        return _dedupe(files)

    def save(self, path: Path) -> None:
        """Save config to TOML file"""
        if tomli_w is None:
            raise RuntimeError(
                "tomli_w not installed, cannot save config "
                "(install with: pip install 'opencode-rate-limiter' or 'tomli-w')"
            )

        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "daemon": {
                "interval_seconds": self.daemon.interval_seconds,
                "models": self.daemon.models,
                "probe_timeout_seconds": self.daemon.probe_timeout_seconds,
                "auto_cleanup_on_429": self.daemon.auto_cleanup_on_429,
                "history_size": self.daemon.history_size,
                "event_history_size": self.daemon.event_history_size,
                **(
                    {"notify_webhook": self.daemon.notify_webhook}
                    if self.daemon.notify_webhook
                    else {}
                ),
                **(
                    {"notify_command": self.daemon.notify_command}
                    if self.daemon.notify_command
                    else {}
                ),
                "respect_cooldown": self.daemon.respect_cooldown,
                "daily_probe_budget": self.daemon.daily_probe_budget,
                "key_cooldown_seconds": self.daemon.key_cooldown_seconds,
            },
            "account_pool": {
                "accounts": self.account_pool.accounts,
                "strategy": self.account_pool.strategy,
                "health_window": self.account_pool.health_window,
                "score_weights": self.account_pool.score_weights,
            },
            "prober": {
                "endpoint": self.prober.endpoint,
                "ping_message": self.prober.ping_message,
                "max_tokens": self.prober.max_tokens,
                "extra_headers": self.prober.extra_headers,
                "http2": self.prober.http2,
                "connection_pool_size": self.prober.connection_pool_size,
                "max_retries": self.prober.max_retries,
                # tomli_w cannot serialize None; omit session_id when unset
                **({"session_id": self.prober.session_id} if self.prober.session_id else {}),
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
