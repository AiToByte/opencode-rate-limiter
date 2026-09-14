"""Human-readable rendering helpers for the check/probe commands.

Extracted from `cli` so presentation logic stays testable in isolation;
`cli` re-exports these names for backward compatibility.
"""

from __future__ import annotations

from typing import Any

from .daemon import load_daemon_state
from .pool import Account, AccountPool, credential_fingerprint
from .prober import ProbeResult

_TREND_LETTERS = {"available": "a", "rate_limited": "!", "error": "x", "unknown": "?"}


def usage_suffix(result: ProbeResult) -> str:
    """Human-readable per-request token cost, e.g. `, 12+1 tok` (or empty)."""
    usage = result.usage or {}
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is None and completion is None:
        total = usage.get("total_tokens")
        return f", {total} tok" if total is not None else ""
    return f", {(prompt or 0)}+{(completion or 0)} tok"


def account_credential_info(pool: AccountPool, account: Account) -> dict[str, str] | None:
    """Resolve a single account's credential for display (kind + fingerprint)."""
    try:
        cred = pool.resolve_credential(account)
    except Exception:
        return None
    if not cred:
        return None
    return {"kind": cred[0], "fingerprint": credential_fingerprint(cred[1])}


def render_trend(history: list[dict[str, Any]], width: int = 30) -> list[str]:
    """Render a per-model status grid over the recorded probe rounds.

    Columns run old -> new; "." marks rounds where a model was not probed
    (e.g. skipped by cooldown or budget).
    """
    rounds = history[-width:]
    models: list[str] = []
    for entry in rounds:
        for model in entry.get("models", {}):
            if model not in models:
                models.append(model)
    lines = []
    name_width = max((len(m) for m in models), default=5)
    for model in models:
        cells = [
            _TREND_LETTERS.get(entry.get("models", {}).get(model, ""), ".") for entry in rounds
        ]
        lines.append(f"    {model:<{name_width}}  {' '.join(cells)}")
    lines.append("    （左→右 = 旧→新；a=可用 !=限流 x=错误 .=未探测）")
    return lines


def render_events(events: list[dict[str, Any]], limit: int = 5) -> list[str]:
    """Render the most recent decision events, newest first."""
    lines = []
    for event in reversed(events[-limit:]):
        ts = str(event.get("ts", ""))[:19]
        extras = ", ".join(f"{k}={v}" for k, v in event.items() if k not in ("ts", "kind"))
        suffix = f" ({extras})" if extras else ""
        lines.append(f"    {ts}Z  {event.get('kind', '?')}{suffix}")
    return lines


def probe_diff_against_daemon(results: list[ProbeResult]) -> str | None:
    """Compare probe results against the daemon's last recorded round.

    Returns a one-line summary of newly-limited / recovered models, or None
    when there is no previous round or nothing changed.
    """
    state = load_daemon_state()
    if not state:
        return None
    prev = state.get("models")
    if not isinstance(prev, dict):
        return None
    prev_status = {
        model: entry.get("status") for model, entry in prev.items() if isinstance(entry, dict)
    }
    now_limited = {r.model for r in results if r.status == "rate_limited"}
    newly_limited = sorted(m for m in now_limited if prev_status.get(m) != "rate_limited")
    recovered = sorted(
        r.model
        for r in results
        if r.status == "available" and prev_status.get(r.model) == "rate_limited"
    )
    if not newly_limited and not recovered:
        return None
    parts = []
    if newly_limited:
        parts.append("新增限流: " + ", ".join(newly_limited))
    if recovered:
        parts.append("已恢复: " + ", ".join(recovered))
    return "与 daemon 上一轮相比 — " + "; ".join(parts)
