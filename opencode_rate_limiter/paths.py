"""Cross-platform path resolution for OpenCode and opencode-rate-limiter state."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from platformdirs import user_config_dir, user_state_dir


def _dedupe(items: list[Path]) -> list[Path]:
    """Order-preserving de-duplication"""
    seen: set[Path] = set()
    unique: list[Path] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


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

    # Linux / other Unix (XDG). macOS and Windows already have their
    # platform branches above and must not also collect XDG paths.
    elif sys.platform != "darwin":
        xdg_config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        dirs.append(Path(xdg_config) / "opencode")
        xdg_state = os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
        dirs.append(Path(xdg_state) / "opencode")

    return _dedupe(dirs)


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


_VERSION_CACHE: dict[str, str] = {"value": "", "at": "0"}
_VERSION_TTL_SECONDS = 300.0


def clear_opencode_version_cache() -> None:
    """Reset the cached `opencode --version` probe (mainly for tests)."""
    _VERSION_CACHE["value"] = ""
    _VERSION_CACHE["at"] = "0"


def get_opencode_version() -> str:
    """Detect OpenCode CLI version

    `OPENCODE_VERSION` env var overrides detection (useful for containers or
    when the opencode binary is not on PATH). Subprocess results are cached
    for 5 minutes; the env override always bypasses the cache.
    """
    import time as _time

    override = os.environ.get("OPENCODE_VERSION")
    if override:
        return override
    try:
        age = _time.monotonic() - float(_VERSION_CACHE["at"])
    except (TypeError, ValueError):
        age = float("inf")
    if _VERSION_CACHE["value"] and age < _VERSION_TTL_SECONDS:
        return _VERSION_CACHE["value"]
    version = _detect_opencode_version()
    if version != "unknown":
        _VERSION_CACHE["value"] = version
        _VERSION_CACHE["at"] = str(_time.monotonic())
    return version


def _detect_opencode_version() -> str:
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
