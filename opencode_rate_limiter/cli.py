"""Command handlers, command dispatch table and the main entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as _dt
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, cast

from .cleanup import CleanupManager, CleanupResult
from .completions import generate_completions
from .config import Config
from .daemon import DaemonLockError, RateLimiterDaemon, load_daemon_state
from .diagnostics import format_report, run_diagnostics
from .errors import ErrorKind, classify_opencode_log_line, explain_kind
from .headers import HeaderInjector
from .logs import level_from_args, setup_logging
from .meta import __version__
from .parser import build_parser, should_print_banner
from .paths import (
    get_opencode_auth_files,
    get_opencode_config_dirs,
    get_opencode_version,
)
from .pool import Account, AccountPool, build_auth_payload, credential_fingerprint
from .prober import ModelProber, ProbeResult
from .render import (
    account_credential_info as _account_credential_info_impl,
)
from .render import (
    probe_diff_against_daemon as _probe_diff_against_daemon_impl,
)
from .render import (
    render_events as _render_events_impl,
)
from .render import (
    render_trend as _render_trend_impl,
)
from .render import (
    usage_suffix,
)
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


def _print_cleanup_result(result: CleanupResult, dry_run: bool = False) -> None:
    for d in result.details:
        print(f"  {d}")
    if result.errors:
        for e in result.errors:
            print(f"  ERROR: {e}")
    if dry_run:
        print(
            f"\nDone: {result.cleared_count} items processed, "
            f"{result.would_clear_count} would be processed, {len(result.errors)} errors"
        )
    else:
        print(f"\nDone: {result.cleared_count} items processed, {len(result.errors)} errors")


def _print_quota_notice() -> None:
    """Honest framing: local operations cannot lift the server-side limit."""
    reset = ModelProber._estimate_reset(None)
    print(
        f"\nNote: the Zen free-tier quota is counted server-side per IP and resets"
        f" at UTC midnight (in ~{reset // 3600}h {(reset % 3600) // 60}m)."
    )
    print("Local operations cannot lift it; this run only backed up auth files.")


async def cmd_quick(config: Config, args: argparse.Namespace) -> int:
    log = logging.getLogger("cmd.quick")
    log.info("Starting quick maintenance", extra={"dry_run": args.dry_run})

    cleanup = CleanupManager(config.cleanup)
    # quick profile: back up auth.json, no cache purge, no token surgery
    result = cleanup.full_cleanup(dry_run=args.dry_run, include_cache=False)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_cleanup_result(result, dry_run=args.dry_run)
        _print_quota_notice()

    return 0 if not result.errors else 1


async def cmd_deep(config: Config, args: argparse.Namespace) -> int:
    log = logging.getLogger("cmd.deep")
    log.info("Starting deep maintenance", extra={"dry_run": args.dry_run})

    cleanup = CleanupManager(config.cleanup)
    # deep profile: auth backups + cache purge (user-configured / native cache dirs)
    result = cleanup.full_cleanup(dry_run=args.dry_run, include_cache=True)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_cleanup_result(result, dry_run=args.dry_run)
        _print_quota_notice()

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
        if name and pool is not None:
            pool.mark_result(
                name,
                success=r.status == "available",
                latency_ms=r.latency_ms,
                error_type=r.error_type,
                error_kind=r.error_kind,
            )

    if args.json:
        payload = [{**r.to_dict(), "account": account_by_model.get(r.model)} for r in results]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        status_icons = {"available": "+", "rate_limited": "!", "error": "x", "unknown": "?"}
        for r in results:
            icon = status_icons.get(r.status, "?")
            account_name = account_by_model.get(r.model)
            tag = f" [account: {account_name}]" if account_name else ""
            print(f"  [{icon}] {r.model}: {r.status} ({r.latency_ms:.0f}ms{usage_suffix(r)}){tag}")
            if r.retry_after:
                print(f"      retry_after: {r.retry_after}s")
            if r.error:
                print(f"      error: {r.error}")

    if not args.json:
        import asyncio as _asyncio

        diff = await _asyncio.to_thread(_probe_diff_against_daemon, results)
        if diff:
            print(f"\n  {diff}")

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

    Backs up the existing file to `<name>.json.bak` first (rotating any
    previous backup to a timestamped copy, same convention as
    CleanupManager.backup_auth_files) and writes the new payload atomically
    (tmp file + os.replace). Returns (status, target_path) where status is
    "applied", "dry_run", or an error string.
    """

    import datetime as _dt_inner

    auth = pool.read_auth(account)
    if not auth:
        return "no_resolvable_auth", None

    candidates = get_opencode_auth_files()
    target = next((p for p in candidates if p.exists()), candidates[0])

    if dry_run:
        return "dry_run", str(target)

    if target.exists():
        backup = target.with_suffix(".json.bak")
        if backup.exists():
            stamp = _dt_inner.datetime.now(_dt_inner.UTC).strftime("%Y%m%dT%H%M%SZ")
            shutil.copy2(backup, target.with_suffix(f".json.bak.{stamp}"))
        shutil.copy2(target, backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = build_auth_payload(auth)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    return "applied", str(target)


async def cmd_rotate(config: Config, args: argparse.Namespace) -> int:
    log = logging.getLogger("cmd.rotate")

    # Override strategy only when the flag was explicitly given (default None
    # keeps the configured strategy).
    strategy = getattr(args, "strategy", None) or config.account_pool.strategy
    config.account_pool.strategy = strategy
    pool = AccountPool(config.account_pool)

    if not pool.accounts:
        log.warning("No accounts configured in account_pool")
        if args.json:
            print(json.dumps({"error": "no accounts configured"}, indent=2))
        else:
            print("No accounts configured. Add accounts to [account_pool] in config.toml")
        return 1

    forced = getattr(args, "to", None)
    if forced:
        next_account = next((a for a in pool.accounts if a.name == forced), None)
        if next_account is None:
            log.error("Unknown account '%s'", forced)
            available = ", ".join(a.name for a in pool.accounts)
            if args.json:
                print(
                    json.dumps(
                        {"error": f"unknown account: {forced}", "accounts": available},
                        indent=2,
                    )
                )
            else:
                print(f"Unknown account: {forced} (available: {available})")
            return 1
        pool.note_served(next_account)
    else:
        next_account = pool.get_next()
    if next_account is None:
        log.warning("Account pool returned no account despite non-empty pool")
        return 1

    credential = pool.resolve_credential(next_account)
    token = credential[1] if credential else None
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
            "strategy": strategy,
            "explicit": bool(forced),
            "dry_run": args.dry_run,
            "auth_token": token is not None,
        },
    )

    if args.json:
        print(
            json.dumps(
                {
                    "rotated_to": next_account.name,
                    "strategy": strategy,
                    "explicit": bool(forced),
                    "accounts": [a.name for a in pool.accounts],
                    "auth_token_resolved": token is not None,
                    "credential": (
                        {
                            "kind": credential[0],
                            "fingerprint": credential_fingerprint(credential[1]),
                        }
                        if credential
                        else None
                    ),
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
        print(f"Strategy: {strategy}" + (" (explicit --to, strategy skipped)" if forced else ""))
        print(f"Available accounts: {', '.join(a.name for a in pool.accounts)}")
        if credential:
            print(f"Credential: {credential[0]} ({credential_fingerprint(credential[1])})")
        else:
            print("Auth token resolved: no")
        if apply_status == "applied":
            print(f"Auth written to: {apply_target}")
        elif apply_status == "dry_run":
            print(f"(dry run) Would write auth to: {apply_target}")
        if args.dry_run:
            print("(dry run) Preview only - selection shown, nothing was changed")

    return 0


def _account_credential_info(config: Config, account: Account) -> dict[str, str] | None:
    """Resolve a single account's credential for display (kind + fingerprint)."""
    pool = AccountPool(config.account_pool)
    return _account_credential_info_impl(pool, account)


_TREND_LETTERS = {"available": "a", "rate_limited": "!", "error": "x", "unknown": "?"}


def _render_trend(history: list[dict[str, Any]], width: int = 30) -> list[str]:
    return _render_trend_impl(history, width)


def _render_events(events: list[dict[str, Any]], limit: int = 5) -> list[str]:
    return _render_events_impl(events, limit)


def _probe_diff_against_daemon(results: list[ProbeResult]) -> str | None:
    return _probe_diff_against_daemon_impl(results)


async def cmd_check(config: Config, args: argparse.Namespace) -> int:
    log = logging.getLogger("cmd.check")
    log.info("Running health check")

    export_path = getattr(args, "export_events", None)
    if export_path:
        return _export_daemon_events(args, export_path)

    version = get_opencode_version()
    shared_pool = AccountPool(config.account_pool) if config.account_pool.accounts else None
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
                {
                    "name": a.name,
                    "auth_path": a.auth_path,
                    "env_var": a.env_var,
                    "credential": (
                        _account_credential_info_impl(shared_pool, a) if shared_pool else None
                    ),
                }
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
    cooldowns = daemon_info.get("cooldowns")
    if cooldowns:
        parts = ", ".join(f"{m}={s}s" for m, s in sorted(cooldowns.items()))
        print(f"  cooldowns        : {parts}")
    events = daemon_info.get("events")
    if getattr(args, "trend", False) and isinstance(events, list) and events:
        print("  recent events    : (newest first)")
        for line in _render_events(events):
            print(line)
    usage = daemon_info.get("probe_usage")
    if isinstance(usage, dict) and usage.get("day"):
        print(
            f"  probe budget     : {usage.get('count', 0)}/{config.daemon.daily_probe_budget}"
            f" used (UTC day {usage['day']})"
        )
    print(
        f"  account pool     : {pool_info['configured_accounts']} account(s)"
        f" | strategy={pool_info['strategy']}"
    )
    for acct in pool_info.get("accounts", []):
        cred = acct.get("credential")
        if cred:
            print(f"    - {acct['name']}: {cred['kind']} ({cred['fingerprint']})")
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
        if getattr(args, "trend", False):
            print("  trend grid       :")
            for line in _render_trend(history):
                print(line)
    print(
        f"  paths            : config_dirs={len(paths_info['config_dirs'])}"
        f" | cache_dirs={len(paths_info['cache_dirs'])}"
        f" | state_files={len(paths_info['state_files'])}"
        f" | auth_files={len(paths_info['auth_files'])}"
    )
    return 0


def _export_daemon_events(args: argparse.Namespace, export_path: Path | str) -> int:
    """Write the daemon's decision-event ring to a file (jsonl/csv) for triage."""
    import csv

    fmt = getattr(args, "export_format", "jsonl")
    if fmt not in ("jsonl", "csv"):
        print(f"Unknown --export-format '{fmt}' (use --export-format jsonl|csv).")
        return 2
    state = load_daemon_state()
    events = state.get("events") if isinstance(state, dict) else None
    if not isinstance(events, list):
        print("No daemon state with events found; is the daemon running?")
        return 1
    target = Path(export_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "jsonl":
        with open(target, "w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
    else:
        columns = ["ts", "kind"]
        for event in events:
            if isinstance(event, dict):
                for key in event:
                    if key not in columns:
                        columns.append(key)
        with open(target, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for event in events:
                writer.writerow(event if isinstance(event, dict) else {})
    print(f"Exported {len(events)} events to {target} ({fmt}).")
    return 0


def _explain_one(text: str) -> dict[str, Any]:
    """Classify one pasted error line into a JSON-serializable explanation."""
    trimmed = text.strip()[:2000]
    result = classify_opencode_log_line(trimmed)
    kind: ErrorKind = result.kind
    title, detail, remedy = explain_kind(kind)
    return {
        "input": trimmed[:500],
        "kind": kind,
        "matched": result.matched,
        "title": title,
        "detail": detail,
        "remedy": remedy,
    }


def _explain_exit(results: list[dict[str, Any]]) -> int:
    kinds = {r["kind"] for r in results}
    if kinds & {"reasoning_replay", "transient_transport", "upstream", "auth", "server"}:
        return 2
    if "rate_limited" in kinds:
        return 1
    return 0


def _read_explain_lines(args: argparse.Namespace) -> list[str] | None:
    """Collect input lines for explain/offline-diagnose; None means no offline input."""
    from_log = getattr(args, "from_log", None)
    if from_log is not None:
        try:
            content = Path(from_log).expanduser().read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"Cannot read log file {from_log}: {e}")
            return []
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        if len(content) > 1_000_000:
            print("Log file too large (>1MB); refusing to parse.")
            return []
        return lines[:50]
    from_text = getattr(args, "from_text", None)
    if from_text:
        return [from_text]
    text = getattr(args, "text", None)
    if text:
        return [text]
    return None


async def cmd_explain(config: Config, args: argparse.Namespace) -> int:
    del config  # offline only: no config, no network, no quota consumed
    lines = _read_explain_lines(args)
    if not lines:
        print('Usage: opencode-rate-limiter explain "<error text>" [--from-log FILE]')
        print("Zero-quota offline classification; nothing is sent to the gateway.")
        return 2
    results = [_explain_one(line) for line in lines]
    if args.json:
        if len(results) == 1:
            print(json.dumps(results[0], indent=2, ensure_ascii=False))
        else:
            summary: dict[str, int] = {}
            for r in results:
                summary[r["kind"]] = summary.get(r["kind"], 0) + 1
            payload = {"results": results, "summary": summary}
            print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for r in results:
            print(f"[{r['kind']}] {r['title']}")
            print(f"  {r['detail']}")
            print(f"  → 建议: {r['remedy']}")
            if len(results) > 1:
                print(f"  原文: {r['input'][:160]}")
    return _explain_exit(results)


async def cmd_diagnose(config: Config, args: argparse.Namespace) -> int:
    offline = _read_explain_lines(args)
    if offline is not None:
        if not offline:
            return 2
        from .diagnostics import Diagnosis, Finding

        results = [_explain_one(line) for line in offline]
        findings = [
            Finding(
                "fail" if r["kind"] != "unknown" else "info",
                r["title"],
                f"{r['detail']}\n原文: {r['input'][:300]}",
                remedy=r["remedy"],
            )
            for r in results
        ]
        kinds = {r["kind"] for r in results}
        if "rate_limited" in kinds and len(kinds) == 1:
            verdict, exit_code = "rate_limited", 1
        elif kinds == {"unknown"}:
            verdict, exit_code = "unknown", 0
        else:
            verdict, exit_code = "error", 2
        if args.json:
            report = Diagnosis(
                timestamp=_dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z"),
                model="offline",
                verdict=verdict,
                exit_code=exit_code,
                probe={"offline_inputs": len(results), "results": results},
                findings=findings,
            )
            print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        else:
            for r in results:
                print(f"[{r['kind']}] {r['title']}")
                print(f"  {r['detail']}")
                print(f"  → 建议: {r['remedy']}")
        return exit_code
    report = await run_diagnostics(config, model=args.model)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(format_report(report))
    return report.exit_code


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
    if getattr(args, "stop", False):
        return await cmd_daemon_stop(args)
    if getattr(args, "once", False):
        try:
            results = await daemon.run_once()
        except DaemonLockError as e:
            log.error("Not probing: %s", e)
            return 1
        except KeyboardInterrupt:
            log.info("Interrupted by user")
            return 130
        limited = [r.model for r in results if r.status == "rate_limited"]
        if not args.json:
            for r in results:
                print(f"  [{r.status}] {r.model} ({r.latency_ms:.0f}ms{usage_suffix(r)})")
            if limited:
                print("  限流模型: " + ", ".join(sorted(limited)))
        else:
            print(json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False))
        return 1 if limited else 0
    try:
        await daemon.run()
    except DaemonLockError as e:
        log.error("Not starting daemon: %s", e)
        return 1
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    return 0


async def cmd_daemon_stop(args: argparse.Namespace) -> int:
    """Stop a running daemon gracefully (SIGTERM + wait, cross-platform)."""
    import signal as _signal

    from .daemon import _pid_alive, get_daemon_lock_path

    log = logging.getLogger("cmd.daemon-stop")
    lock_path = get_daemon_lock_path()
    pid: int | None = None
    try:
        text = lock_path.read_text(encoding="utf-8").strip()
        pid = int(text)
    except (OSError, ValueError):
        pid = None
    if pid is None:
        msg = {"running": False, "reason": "no lock file"}
        if args.json:
            print(json.dumps(msg, indent=2))
        else:
            print("Daemon does not appear to be running (no lock file).")
        return 0
    if not _pid_alive(pid):
        with contextlib.suppress(OSError):
            lock_path.unlink()
        msg = {"running": False, "pid": pid, "reason": "stale lock removed"}
        if args.json:
            print(json.dumps(msg, indent=2))
        else:
            print(f"Removed stale lock (pid {pid} not alive).")
        return 0
    if pid == os.getpid():
        log.error("Lock file belongs to this process; refusing to stop ourselves")
        if not args.json:
            print("Lock file belongs to this process; refusing to stop ourselves.")
        return 1
    log.info("Stopping daemon", extra={"pid": pid})
    try:
        os.kill(pid, _signal.SIGTERM)
    except (OSError, ValueError) as e:
        log.error("Failed to signal daemon (pid %s): %s", pid, e)
        return 1
    deadline = time.monotonic() + 10.0
    while _pid_alive(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
    stopped = not _pid_alive(pid)
    if args.json:
        print(json.dumps({"running": not stopped, "pid": pid}, indent=2))
    elif stopped:
        print(f"Daemon (pid {pid}) stopped.")
    else:
        print(f"Daemon (pid {pid}) still alive after 10s; stop it manually.")
    return 0 if stopped else 1


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
    "diagnose": cmd_diagnose,
    "explain": cmd_explain,
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

    setup_logging(
        level_from_args(args),
        args.json,
        log_file=getattr(args, "log_file", None),
        json_verbose=getattr(args, "json_verbose", False),
    )

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
