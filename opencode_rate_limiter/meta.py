"""Package metadata."""

from __future__ import annotations

# Canonical single source of the version: pyproject.toml, man page, MANUAL,
# docs headers and README must all match this (enforced in CI by
# scripts/check_version.py). Deliberately a static string — not
# importlib.metadata — so the version is available without an install and
# check_version.py can regex it.
__version__ = "0.7.0"
