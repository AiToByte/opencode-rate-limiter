#!/usr/bin/env python3
"""PyInstaller build script for opencode-rate-limiter."""

import platform
import shutil
import subprocess
import sys
from pathlib import Path


def binary_base_name() -> str:
    """Return the versioned, platform-tagged binary stem."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return f"opencode-rate-limiter-windows-{machine}"
    if system == "darwin":
        return f"opencode-rate-limiter-macos-{machine}"
    return f"opencode-rate-limiter-linux-{machine}"


def main() -> int:
    project_root = Path(__file__).parent.parent
    src = project_root / "opencode_rate_limiter.py"
    dist_dir = project_root / "dist"
    dist_dir.mkdir(exist_ok=True)

    base_name = binary_base_name()
    binary_name = f"{base_name}.exe" if platform.system().lower() == "windows" else base_name
    output_path = dist_dir / binary_name

    # Clean previous build artifacts
    for pattern in ["build", "*.spec", "__pycache__"]:
        for p in project_root.glob(pattern):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)

    # PyInstaller arguments
    import PyInstaller.__main__

    pyinstaller_args = [
        str(src),
        "--onefile",
        "--name",
        binary_name,
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(project_root / "build"),
        "--specpath",
        str(project_root),
        "--clean",
        "--noconfirm",
        "--optimize",
        "2",
        "--hidden-import",
        "tomli",
        "--hidden-import",
        "tomli_w",
        "--hidden-import",
        "platformdirs",
        "--hidden-import",
        "httpx",
        "--collect-all",
        "platformdirs",
    ]
    # --strip is only meaningful on Unix linkers and is ignored on Windows.
    if platform.system().lower() != "windows":
        pyinstaller_args.append("--strip")
    if platform.system().lower() == "darwin":
        pyinstaller_args.extend(["--osx-bundle-identifier", "com.opencode.ratelimiter"])

    print(f"Building binary: {binary_name}")
    print(f"Source: {src}")
    print(f"Output: {output_path}")
    print(f"PyInstaller args: {' '.join(pyinstaller_args)}")

    try:
        PyInstaller.__main__.run(pyinstaller_args)
    except SystemExit as e:
        if e.code != 0:
            print(f"PyInstaller failed with exit code {e.code}")
            return e.code if isinstance(e.code, int) else 1

    # Verify binary exists and is executable
    if output_path.exists():
        if platform.system().lower() != "windows":
            output_path.chmod(0o755)

        # Quick smoke test
        result = subprocess.run(
            [str(output_path), "--help"], capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            print(f"\n[OK] Build successful: {output_path}")
            print(f"     Size: {output_path.stat().st_size / 1024 / 1024:.1f} MB")
            return 0
        print("\n[FAIL] Binary verification failed:")
        print(result.stderr)
        return 1
    print(f"\n[FAIL] Binary not found at {output_path}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
