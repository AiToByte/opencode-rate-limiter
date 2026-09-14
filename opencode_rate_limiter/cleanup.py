"""Auth backup and cache maintenance.

Note on scope (verified against the opencode source, 2026-09): the Zen free
tier is rate-limited **server-side** (per-IP daily counters in Redis, reset at
UTC midnight). The opencode CLI keeps no local rate-limit state, so nothing
deleted locally can lift a server-side limit. This module therefore only
performs safe, real operations: backing up auth.json and purging cache
directories.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import CleanupConfig
from .paths import _dedupe, get_opencode_auth_files, get_opencode_native_cache_dirs


def _expand_path(path_str: str) -> Path:
    expanded = Path(path_str).expanduser()
    return Path(os.path.expandvars(str(expanded)))


def _timestamp_suffix() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")


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
    """Auth backup and cache maintenance for OpenCode installations"""

    def __init__(self, config: CleanupConfig):
        self.config = config
        self.log = logging.getLogger("cleanup")

    def resolve_cache_dirs(self) -> list[Path]:
        """Configured cache dirs (expanded) + OpenCode native dirs, deduped."""
        custom = [_expand_path(p) for p in self.config.cache_dirs]
        return _dedupe([*custom, *get_opencode_native_cache_dirs()])

    def backup_auth_files(
        self, auth_files: list[Path] | None = None, dry_run: bool = False
    ) -> CleanupResult:
        """Back up auth.json files (contents untouched).

        The latest backup is always `<name>.json.bak`; when that file already
        exists it is first rotated to a timestamped copy so history is kept.
        """
        result = CleanupResult()
        targets = auth_files or get_opencode_auth_files()

        for auth_file in targets:
            if not auth_file.exists():
                continue

            if dry_run:
                result.details.append(f"DRY RUN: Would back up {auth_file}")
                result.cleared_count += 1
                continue

            try:
                backup_path = auth_file.with_suffix(".json.bak")
                if backup_path.exists():
                    rotated = auth_file.with_suffix(f".json.bak.{_timestamp_suffix()}")
                    shutil.copy2(backup_path, rotated)
                    result.details.append(f"Rotated previous backup to {rotated.name}")
                shutil.copy2(auth_file, backup_path)
                result.cleared_count += 1
                result.details.append(f"Backed up {auth_file.name} to {backup_path.name}")
                self.log.info("Backed up auth file: %s", auth_file)
            except Exception as e:
                result.errors.append(f"Failed to back up {auth_file}: {e}")
                self.log.warning("Failed to back up %s: %s", auth_file, e)

        return result

    def purge_cache(
        self, cache_dirs: list[Path] | None = None, dry_run: bool = False
    ) -> CleanupResult:
        """Remove and recreate cache directories"""
        result = CleanupResult()
        targets = list(cache_dirs) if cache_dirs is not None else self.resolve_cache_dirs()

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
        """Run maintenance operations

        `include_cache=False` limits the pass to auth backups (the `quick`
        profile); cache purging is the `deep` profile. Neither affects the
        server-side free-tier quota.
        """
        result = CleanupResult()

        r1 = self.backup_auth_files(dry_run=dry_run)
        result.cleared_count += r1.cleared_count
        result.errors.extend(r1.errors)
        result.details.extend(r1.details)

        if include_cache:
            r2 = self.purge_cache(dry_run=dry_run)
            result.cleared_count += r2.cleared_count
            result.errors.extend(r2.errors)
            result.details.extend(r2.details)

        return result
