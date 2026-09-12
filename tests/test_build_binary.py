"""Binary build script tests - Phase 6."""

import importlib.util
import platform
from pathlib import Path
from typing import Any

BINARY_PATH = Path(__file__).resolve().parent.parent / "scripts" / "build_binary.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("build_binary", BINARY_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load build_binary.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_binary_base_name_matches_platform() -> None:
    module = _load_module()
    name = module.binary_base_name()
    system = platform.system().lower()
    machine = platform.machine().lower()
    assert name.startswith(f"opencode-rate-limiter-{system}-{machine}")


def test_binary_main_returns_int() -> None:
    module = _load_module()
    assert callable(module.main)
    assert callable(module.binary_base_name)


def test_build_script_uses_package_entry() -> None:
    """包化后构建脚本必须经由入口 shim，不能再指向已删除的单文件"""
    source = BINARY_PATH.read_text(encoding="utf-8")
    assert "opencode_rate_limiter.py" not in source
    assert "_pyinstaller_entry" in source
    assert "--paths" in source.replace(" ", "")
