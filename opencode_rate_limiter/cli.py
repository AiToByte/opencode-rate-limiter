"""Command handlers, command dispatch table and the main entry point."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import shutil
from pathlib import Path
from typing import Any, cast

from .cleanup import CleanupManager, CleanupResult
from .completions import generate_completions
from .config import Config
from .daemon import DaemonLockError, RateLimiterDaemon, load_daemon_state
from .headers import HeaderInjector
from .logs import level_from_args, setup_logging
from .meta import __version__
from .parser import build_parser, should_print_banner
from .paths import (
    get_opencode_auth_files,
    get_opencode_config_dirs,
    get_opencode_version,
)
from .pool import Account, AccountPool
from .prober import ModelProber
from .service import (
    _default_config_path_str,
    generate_launchd_plist,
    generate_systemd_unit,
    generate_task_xml,
)


def print_banner() -> None:
    print("=" * 60)
    print(f"      OpenCode 免费模型 Rate-Limit 缓解工具 v{__version__}")
    print("=" * 60)


def _print_cleanup_result(result: CleanupResult) -> None:
    for d in result.details:
        print(f"  {d}")
    if result.errors:
        for e in result.errors:
            print(f"  ERROR: {e}")
    print(f"\nDone: {result.cleared_count} items processed, {len(result.errors)} errors")


async def cmd_quick(config: Config, args: argparse.Namespace) -> int:
    log = logging.getLogger("cmd.quick")
    log.info("Starting quick cleanup", extra={"dry_run": args.dry_run})

    cleanup = CleanupManager(config.cleanup)
    # quick profile: rate-limit locks + state.json + auth token reset, no cache purge
    result = cleanup.full_cleanup(dry_run=args.dry_run, include_cache=False)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_cleanup_result(result)

    return 0 if not result.errors else 1


async def cmd_deep(config: Config, args: argparse.Namespace) -> int:
    log = logging.getLogger("cmd.deep")
    log.info("Starting deep cleanup", extra={"dry_run": args.dry_run})

    cleanup = CleanupManager(config.cleanup)
    # deep profile: quick + cache purge; tokens are cleared so a re-login is expected
    result = cleanup.full_cleanup(dry_run=args.dry_run, include_cache=True)

    if args.json:
        data = result.to_dict()
        data["relogin_hint"] = "run `opencode login` to refresh the cleared tokens"
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        _print_cleanup_result(result)
        print("\nTokens were cleared - run `opencode login` to authenticate again.")

    return 0 if not result.errors else 1


async def cmd_probe(config: Config, args: argparse.Namespace) -> int:
    log = logging.getLogger("cmd.probe")
    version = get_opencode_version()
    injector = HeaderInjector(config.headers, version)
    prober = ModelProber(config.daemon.probe_timeout_seconds, config.prober)

    models = config.daemon.models if args.model == "all" else [args.model]
    headers = injector.build_headers()

    # Per-model account rotation: each probed model carries the auth token of
    # the account selected for it (round-robin/least_used/health), so results
    # feed back into the pool's health tracking.
    pool = AccountPool(config.account_pool) if config.account_pool.accounts else None
    headers_by_model: dict[str, dict[str, str]] = {}
    account_by_model: dict[str, str] = {}
    if pool is not None:
        for model in models:
            account = pool.get_next()
            if account is None:
                continue
            account_by_model[model] = account.name
            token = pool.resolve_token(account)
            if token:
                headers_by_model[model] = injector.build_headers(token=token)
        if headers_by_model:
            log.info(
                "Probe with account auth",
                extra={"accounts": sorted(set(account_by_model.values()))},
            )

    log.info("Probing %d models", len(models), extra={"models": models})
    results = await prober.probe_all(models, headers, headers_by_model or None)

    for r in results:
        name = account_by_model.get(r.model)
        if name:
            pool.mark_result(name, success=r.status == "available", latency_ms=r.latency_ms)  # type: ignore[union-attr]

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False))
    else:
        status_icons = {"available": "+", "rate_limited": "!", "error": "x", "unknown": "?"}
        for r in results:
            icon = status_icons.get(r.status, "?")
            print(f"  [{icon}] {r.model}: {r.status} ({r.latency_ms:.0f}ms)")
            if r.retry_after:
                print(f"      retry_after: {r.retry_after}s")
            if r.error:
                print(f"      error: {r.error}")

    any_limited = any(r.status == "rate_limited" for r in results)
    return 1 if any_limited else 0


async def cmd_headers(config: Config, args: argparse.Namespace) -> int:
    version = get_opencode_version()
    injector = HeaderInjector(config.headers, version)

    if args.export:
        print(injector.to_env_export(args.model))
    else:
        headers = injector.build_headers(args.model)
        print(json.dumps(headers, indent=2, ensure_ascii=False))

    return 0


def _apply_account_auth(
    pool: AccountPool, account: Account, dry_run: bool
) -> tuple[str, str | None]:
    """Write the account's auth JSON into the active OpenCode auth.json

    Backs up the existing file to `<name>.json.bak` first (same convention as
    CleanupManager.rotate_auth_tokens). Returns (status, target_path) where
    status is "applied", "dry_run", or an error string.
    """

    auth = pool.read_auth(account)
    if not auth:
        return "no_resolvable_auth", None

    candidates = get_opencode_auth_files()
    target = next((p for p in candidates if p.exists()), candidates[0])

    if dry_run:
        return "dry_run", str(target)

    if target.exists():
        backup = target.with_suffix(".json.bak")
        shutil.copy(target, backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(auth, indent=2, ensure_ascii=False), encoding="utf-8")
    return "applied", str(target)


async def cmd_rotate(config: Config, args: argparse.Namespace) -> int:
    log = logging.getLogger("cmd.rotate")

    # Override strategy if provided via CLI
    config.account_pool.strategy = args.strategy
    pool = AccountPool(config.account_pool)

    if not pool.accounts:
        log.warning("No accounts configured in account_pool")
        if args.json:
            print(json.dumps({"error": "no accounts configured"}, indent=2))
        else:
            print("No accounts configured. Add accounts to [account_pool] in config.toml")
        return 1

    next_account = pool.get_next()
    if next_account is None:
        log.warning("Account pool returned no account despite non-empty pool")
        return 1

    token = pool.resolve_token(next_account)
    apply_status: str | None = None
    apply_target: str | None = None
    if args.apply:
        apply_status, apply_target = _apply_account_auth(pool, next_account, args.dry_run)
        if apply_status == "no_resolvable_auth":
            log.error("Cannot apply: account '%s' has no resolvable auth JSON", next_account.name)
            return 1
        log.info(
            "Apply auth",
            extra={"account": next_account.name, "target": apply_target, "status": apply_status},
        )

    log.info(
        "Rotating to account",
        extra={
            "account": next_account.name,
            "strategy": args.strategy,
            "dry_run": args.dry_run,
            "auth_token": token is not None,
        },
    )

    if args.json:
        print(
            json.dumps(
                {
                    "rotated_to": next_account.name,
                    "strategy": args.strategy,
                    "accounts": [a.name for a in pool.accounts],
                    "auth_token_resolved": token is not None,
                    "dry_run": bool(args.dry_run),
                    "applied": apply_status,
                    "auth_target": apply_target,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(f"Rotated to account: {next_account.name}")
        print(f"Strategy: {args.strategy}")
        print(f"Available accounts: {', '.join(a.name for a in pool.accounts)}")
        print(f"Auth token resolved: {'yes' if token else 'no'}")
        if apply_status == "applied":
            print(f"Auth written to: {apply_target}")
        elif apply_status == "dry_run":
            print(f"(dry run) Would write auth to: {apply_target}")
        if args.dry_run:
            print("(dry run) Preview only - selection shown, nothing was changed")

    return 0


async def cmd_check(config: Config, args: argparse.Namespace) -> int:
    log = logging.getLogger("cmd.check")
    log.info("Running health check")

    version = get_opencode_version()
    health = {
        "timestamp": _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z"),
        "config_valid": True,
        "opencode_version": version,
        "config_paths": {
            "config_dirs": [str(p) for p in get_opencode_config_dirs()],
            "cache_dirs": [str(p) for p in config.get_cache_dirs()],
            "state_files": [str(p) for p in config.get_state_files()],
            "auth_files": [str(p) for p in get_opencode_auth_files()],
        },
        "account_pool": {
            "configured_accounts": len(config.account_pool.accounts),
            "strategy": config.account_pool.strategy,
            "accounts": [
                {"name": a.name, "auth_path": a.auth_path, "env_var": a.env_var}
                for a in [Account(**acc) for acc in config.account_pool.accounts]
            ],
        },
        "daemon": {
            "interval_seconds": config.daemon.interval_seconds,
            "models": config.daemon.models,
            "auto_cleanup_on_429": config.daemon.auto_cleanup_on_429,
        },
    }

    state = load_daemon_state()
    if state is not None:
        # Merge runtime daemon status over the configured daemon defaults
        merged = dict(cast("dict[str, Any]", health["daemon"]))
        merged.update(state)
        health["daemon"] = merged
        # Surface per-account health tracked by the daemon, if any (moved out
        # of the daemon section to account_pool.health)
        pool_health = merged.pop("pool_health", None)
        if pool_health:
            cast("dict[str, Any]", health["account_pool"])["health"] = pool_health

    if args.json:
        print(json.dumps(health, indent=2, ensure_ascii=False))
        return 0

    # Human-readable summary (--json for the full machine-readable report)
    daemon_info = cast("dict[str, Any]", health["daemon"])
    pool_info = cast("dict[str, Any]", health["account_pool"])
    paths_info = cast("dict[str, Any]", health["config_paths"])

    print("OpenCode Rate Limiter - Health Check")
    print(f"  opencode version : {version}")
    print("  config           : valid")
    print(
        f"  daemon           : interval={daemon_info['interval_seconds']}s"
        f" | models={len(daemon_info['models'])}"
        f" | auto_cleanup_on_429={'yes' if daemon_info['auto_cleanup_on_429'] else 'no'}"
    )
    if "running" in daemon_info:
        print(
            f"  daemon runtime   : running={'yes' if daemon_info['running'] else 'no'}"
            f" (pid {daemon_info.get('pid', '?')})"
            f" | cycles={daemon_info.get('total_cycles', 0)}"
            f" | uptime={daemon_info.get('uptime_seconds', 0)}s"
        )
    print(
        f"  account pool     : {pool_info['configured_accounts']} account(s)"
        f" | strategy={pool_info['strategy']}"
    )
    pool_health = pool_info.get("health")
    if pool_health:
        print("  account health   :")
        for name, h in sorted(pool_health.items()):
            print(
                f"    - {name}: success={h.get('success', 0)}/{h.get('total', 0)}"
                f" | failures={h.get('consecutive_failures', 0)}"
                f" | avg={h.get('avg_latency_ms', 0)}ms | score={h.get('score', 0)}"
            )
    results = daemon_info.get("models")
    if isinstance(results, dict) and results:
        print("  last probe       :")
        for name, r in sorted(results.items()):
            retry = f", retry_after={r['retry_after']}s" if r.get("retry_after") else ""
            print(f"    [{r['status']}] {name} ({r['latency_ms']}ms{retry})")
    history = daemon_info.get("history")
    if isinstance(history, list) and history:
        tally: dict[str, dict[str, int]] = {}
        for entry in history:
            for model, status in entry.get("models", {}).items():
                tally.setdefault(model, {}).setdefault(status, 0)
                tally[model][status] += 1
        print(f"  probe history    : last {len(history)} probes")
        for model in sorted(tally):
            counts = sorted(tally[model].items(), key=lambda kv: -kv[1])
            parts = ", ".join(f"{status} x{count}" for status, count in counts)
            print(f"    {model}: {parts}")
    print(
        f"  paths            : config_dirs={len(paths_info['config_dirs'])}"
        f" | cache_dirs={len(paths_info['cache_dirs'])}"
        f" | state_files={len(paths_info['state_files'])}"
        f" | auth_files={len(paths_info['auth_files'])}"
    )
    return 0


async def cmd_daemon(config: Config, args: argparse.Namespace) -> int:
    log = logging.getLogger("cmd.daemon")

    if args.dry_run:
        log.info(
            "DRY RUN: Would start daemon",
            extra={"interval": args.interval, "models": args.models},
        )
        return 0

    interval_override = args.interval
    if interval_override is not None and interval_override < 5:
        log.error("interval must be >= 5 seconds, got %d", interval_override)
        return 2

    models_override: list[str] | None = None
    if args.models:
        models_override = [m.strip() for m in args.models.split(",") if m.strip()]
        if not models_override:
            log.error("--models produced an empty model list")
            return 2

    daemon = RateLimiterDaemon(
        config,
        config_path=args.config,
        interval_override=interval_override,
        models_override=models_override,
    )
    try:
        await daemon.run()
    except DaemonLockError as e:
        log.error("Not starting daemon: %s", e)
        return 1
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    return 0


async def cmd_generate_systemd(config: Config, args: argparse.Namespace) -> int:
    print(generate_systemd_unit())
    return 0


async def cmd_generate_launchd(config: Config, args: argparse.Namespace) -> int:
    print(generate_launchd_plist())
    return 0


async def cmd_generate_task(config: Config, args: argparse.Namespace) -> int:
    print(generate_task_xml())
    return 0


async def cmd_generate_config(config: Config, args: argparse.Namespace) -> int:
    log = logging.getLogger("cmd.generate-config")
    target = args.config or Path(_default_config_path_str())

    if target.exists() and not args.force:
        log.error("Config file already exists: %s (use --force to overwrite)", target)
        return 1

    try:
        # Write the built-in defaults as a starting template for the user
        Config().save(target)
    except (RuntimeError, OSError) as e:
        log.error("Failed to write config: %s", e)
        return 1

    if args.json:
        print(json.dumps({"path": str(target), "written": True}, indent=2))
    else:
        print(f"Config written to {target}")
        print("Edit [account_pool] to add accounts before using `rotate` / `daemon`.")
    return 0


async def cmd_completions(config: Config, args: argparse.Namespace) -> int:
    print(generate_completions(args.shell))
    return 0


COMMAND_HANDLERS = {
    "quick": cmd_quick,
    "deep": cmd_deep,
    "probe": cmd_probe,
    "headers": cmd_headers,
    "rotate": cmd_rotate,
    "check": cmd_check,
    "daemon": cmd_daemon,
    "generate-systemd": cmd_generate_systemd,
    "generate-launchd": cmd_generate_launchd,
    "generate-task": cmd_generate_task,
    "generate-config": cmd_generate_config,
    "completions": cmd_completions,
}


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    setup_logging(level_from_args(args), args.json)

    if args.dry_run:
        logging.getLogger("main").info("DRY RUN MODE - no changes will be made")

    try:
        config = Config.load(args.config)
    except Exception as e:
        logging.getLogger("main").error("Failed to load config: %s", e)
        return 2

    if should_print_banner(args.command, args.json):
        print_banner()

    handler = COMMAND_HANDLERS.get(args.command)
    if handler:
        import asyncio

        try:
            return asyncio.run(handler(config, args))
        except KeyboardInterrupt:
            logging.getLogger("main").info("Interrupted by user")
            return 130
        except Exception as e:
            logging.getLogger("main").error("Command failed: %s", e, exc_info=args.verbose >= 1)
            return 1

    parser.print_help()
    return 1
