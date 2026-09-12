"""Test configuration and fixtures."""

from pathlib import Path

import pytest


@pytest.fixture
def temp_config_dir(tmp_path: Path) -> Path:
    """临时配置目录"""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return config_dir


@pytest.fixture
def sample_toml_config(temp_config_dir: Path) -> Path:
    """示例 TOML 配置文件"""
    config_file = temp_config_dir / "config.toml"
    config_file.write_text("""
[daemon]
interval_seconds = 60
models = ["deepseek-v4-flash-free", "nemotron-3-ultra-free"]
probe_timeout_seconds = 15.0
auto_cleanup_on_429 = true

[account_pool]
accounts = [
    { name = "primary", auth_path = "~/.opencode/auth.json" },
    { name = "backup", auth_path = "~/.config/opencode/auth-backup.json" }
]
strategy = "health"

[headers]
user_agent = "opencode/{version}"
x_opencode_client = "opencode-cli"
x_opencode_version = "{version}"

[cleanup]
cache_dirs = ["~/.opencode/cache"]
state_files = ["~/.opencode/state.json"]
preserve_config = true
""")
    return config_file


@pytest.fixture
def invalid_toml_config(temp_config_dir: Path) -> Path:
    """无效 TOML 配置文件"""
    config_file = temp_config_dir / "invalid.toml"
    config_file.write_text("invalid = toml [")
    return config_file


@pytest.fixture
def minimal_toml_config(temp_config_dir: Path) -> Path:
    """最小 TOML 配置文件"""
    config_file = temp_config_dir / "minimal.toml"
    config_file.write_text("""
[daemon]
interval_seconds = 120
""")
    return config_file
