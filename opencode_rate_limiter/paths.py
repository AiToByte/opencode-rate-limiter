"""Cross-platform path resolution for OpenCode and opencode-rate-limiter state."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from platformdirs import user_config_dir, user_state_dir


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
