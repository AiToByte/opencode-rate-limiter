"""Filesystem cleanup for rate-limit locks, state files, auth tokens and caches."""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import CleanupConfig
from .paths import (
    get_opencode_auth_files,
    get_opencode_native_cache_dirs,
    get_opencode_native_state_files,
)


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
    """File system cleanup for rate limit state, auth, and cache"""

    def __init__(self, config: CleanupConfig):
        self.config = config
        self.log = logging.getLogger("cleanup")

    def reset_rate_limit_state(
        self, state_files: list[Path] | None = None, dry_run: bool = False
    ) -> CleanupResult:
        """Remove rate limit lock files and state.json"""
        result = CleanupResult()
        targets = state_files or get_opencode_native_state_files()

        for path in targets:
            # Also look for rate_limit lock files in parent directories
            parent = path.parent
            if parent.is_dir():
                for lock_file in parent.glob("*rate_limit*.json"):
                    if dry_run:
                        result.details.append(f"DRY RUN: Would remove {lock_file}")
                        result.cleared_count += 1
                        continue
                    try:
                        lock_file.unlink()
                        result.cleared_count += 1
                        result.details.append(f"Removed {lock_file.name}")
                        self.log.info("Removed rate limit lock: %s", lock_file)
                    except Exception as e:
                        result.errors.append(f"Failed to remove {lock_file}: {e}")
                        self.log.warning("Failed to remove %s: %s", lock_file, e)

            if path.name == "state.json" and path.exists():
                if dry_run:
                    result.details.append(f"DRY RUN: Would remove {path}")
                    result.cleared_count += 1
                    continue
                try:
                    path.unlink()
                    result.cleared_count += 1
                    result.details.append(f"Removed {path.name}")
                    self.log.info("Removed state file: %s", path)
                except Exception as e:
                    result.errors.append(f"Failed to remove {path}: {e}")
                    self.log.warning("Failed to remove %s: %s", path, e)

        return result

    def rotate_auth_tokens(
        self, auth_files: list[Path] | None = None, dry_run: bool = False
    ) -> CleanupResult:
        """Backup and clear access_token from auth files"""

        result = CleanupResult()
        targets = auth_files or get_opencode_auth_files()

        for auth_file in targets:
            if not auth_file.exists():
                continue

            if dry_run:
                result.details.append(f"DRY RUN: Would rotate {auth_file}")
                result.cleared_count += 1
                continue

            try:
                # Backup
                backup_path = auth_file.with_suffix(".json.bak")
                import shutil

                shutil.copy(auth_file, backup_path)
                result.details.append(f"Backed up to {backup_path.name}")

                # Clear token
                with open(auth_file, "r+", encoding="utf-8") as f:
                    data = json.load(f)
                    if "access_token" in data:
                        data["access_token"] = ""
                    if "rate_limited_until" in data:
                        del data["rate_limited_until"]
                    f.seek(0)
                    json.dump(data, f, indent=2)
                    f.truncate()

                result.cleared_count += 1
                result.details.append(f"Cleared tokens in {auth_file.name}")
                self.log.info("Rotated auth tokens: %s", auth_file)

            except json.JSONDecodeError:
                try:
                    auth_file.unlink()
                    result.details.append(f"Removed corrupt {auth_file.name}")
                    self.log.warning("Removed corrupt auth file: %s", auth_file)
                except Exception as e:
                    result.errors.append(f"Failed to remove corrupt {auth_file}: {e}")
            except Exception as e:
                result.errors.append(f"Failed to rotate {auth_file}: {e}")
                self.log.warning("Failed to rotate %s: %s", auth_file, e)

        return result

    def purge_cache(
        self, cache_dirs: list[Path] | None = None, dry_run: bool = False
    ) -> CleanupResult:
        """Remove cache directories"""

        result = CleanupResult()
        targets = cache_dirs or get_opencode_native_cache_dirs()

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
        """Run cleanup operations

        `include_cache=False` limits the pass to rate-limit locks + state.json
        + auth token reset (the `quick` profile); cache purging is the `deep`
        profile.
        """
        result = CleanupResult()

        r1 = self.reset_rate_limit_state(dry_run=dry_run)
        result.cleared_count += r1.cleared_count
        result.errors.extend(r1.errors)
        result.details.extend(r1.details)

        r2 = self.rotate_auth_tokens(dry_run=dry_run)
        result.cleared_count += r2.cleared_count
        result.errors.extend(r2.errors)
        result.details.extend(r2.details)

        if include_cache:
            r3 = self.purge_cache(dry_run=dry_run)
            result.cleared_count += r3.cleared_count
            result.errors.extend(r3.errors)
            result.details.extend(r3.details)

        return result
