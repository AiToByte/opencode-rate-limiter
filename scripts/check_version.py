#!/usr/bin/env python3
"""Assert that all release version markers agree.

Checks three places (see MANUAL §11.3):
1. opencode_rate_limiter/meta.py   -> __version__
2. pyproject.toml                  -> [project].version
3. man/opencode-rate-limiter.1     -> "opencode-rate-limiter <version>" header

Usage: python scripts/check_version.py [EXPECTED]
Exit codes: 0 = all agree (and match EXPECTED if given), 1 = mismatch.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def meta_version() -> str:
    text = (ROOT / "opencode_rate_limiter" / "meta.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("FAIL: __version__ not found in opencode_rate_limiter/meta.py")
    return match.group(1)


def pyproject_version() -> str:
    with open(ROOT / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    version = data.get("project", {}).get("version")
    if not version:
        raise SystemExit("FAIL: project.version not found in pyproject.toml")
    return str(version)


def man_version() -> str:
    text = (ROOT / "man" / "opencode-rate-limiter.1").read_text(encoding="utf-8")
    match = re.search(r"opencode-rate-limiter\s+(\d+\.\d+\.\d+)", text)
    if not match:
        raise SystemExit("FAIL: version not found in man/opencode-rate-limiter.1 header")
    return match.group(1)


def main() -> int:
    versions = {
        "opencode_rate_limiter/meta.py": meta_version(),
        "pyproject.toml": pyproject_version(),
        "man/opencode-rate-limiter.1": man_version(),
    }
    expected = sys.argv[1] if len(sys.argv) > 1 else None
    ok = True
    values = list(versions.values())
    if len(set(values)) != 1:
        ok = False
    if expected and values[0] != expected:
        ok = False

    for name, version in versions.items():
        marker = "OK " if version == values[0] else "BAD"
        print(f"[{marker}] {name}: {version}")
    if expected:
        print(f"{'OK ' if values[0] == expected else 'BAD'} expected: {expected}")

    if not ok:
        print("\nFAIL: version markers disagree")
        return 1
    print("\nAll version markers agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
