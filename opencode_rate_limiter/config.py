"""Configuration model: TOML + env loading, merging, validation, path expansion."""

from __future__ import annotations

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
            return int(new_value)
        if isinstance(current_value, float):
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
