"""Tests for the v0.6.0 observability round (C-track)."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import pytest


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_json_formatter_lean_by_default(capsys) -> None:
    from opencode_rate_limiter import JSONFormatter

    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("test_lean")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.info("hello")
    logger.removeHandler(handler)

    data = json.loads(capsys.readouterr().err)
    assert data["message"] == "hello"
    assert "module" not in data
    assert "function" not in data
    assert "line" not in data


def test_json_formatter_verbose_has_location(capsys) -> None:
    from opencode_rate_limiter import JSONFormatter

    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter(verbose=True))
    logger = logging.getLogger("test_verbose_loc")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.info("hello")
    logger.removeHandler(handler)

    data = json.loads(capsys.readouterr().err)
    assert data["module"] == "test_0_6_0"
    assert data["function"] == "test_json_formatter_verbose_has_location"
    assert isinstance(data["line"], int)


def test_json_formatter_attaches_traceback(capsys) -> None:
    from opencode_rate_limiter import JSONFormatter

    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("test_exc")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("failed")
    logger.removeHandler(handler)

    data = json.loads(capsys.readouterr().err)
    assert data["message"] == "failed"
    assert "ValueError: boom" in data["exc"]
    assert "Traceback" in data["exc"]


def test_setup_logging_writes_rotating_file(tmp_path: Path) -> None:
    from opencode_rate_limiter import setup_logging

    target = tmp_path / "logs" / "app.log"
    root = logging.root
    old_handlers, old_level = root.handlers[:], root.level
    try:
        setup_logging(logging.INFO, json_output=True, log_file=target)
        assert target.exists()
        logging.getLogger("test_file").info("to file", extra={"k": "v"})
        for h in root.handlers:
            h.flush()
        rows = _read_json_lines(target)
        assert len(rows) == 1
        assert rows[0]["message"] == "to file"
        assert rows[0]["k"] == "v"
    finally:
        for h in root.handlers:
            h.close()
        root.handlers = old_handlers
        root.setLevel(old_level)


def test_setup_logging_file_handler_rotates(tmp_path: Path) -> None:
    from logging.handlers import RotatingFileHandler

    from opencode_rate_limiter import setup_logging

    target = tmp_path / "app.log"
    root = logging.root
    old_handlers, old_level = root.handlers[:], root.level
    try:
        setup_logging(logging.CRITICAL + 1, json_output=False, log_file=target)
        file_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        assert len(file_handlers) == 1
        assert file_handlers[0].maxBytes == 1_000_000
        assert file_handlers[0].backupCount == 3
    finally:
        for h in root.handlers:
            h.close()
        root.handlers = old_handlers
        root.setLevel(old_level)


def _check_ns(**over: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "json": False,
        "trend": False,
        "export_events": None,
        "export_format": "jsonl",
    }
    base.update(over)
    return argparse.Namespace(**base)


def _write_state(tmp_path: Path, events: list[Any]) -> Path:
    from opencode_rate_limiter.daemon import write_daemon_state

    state = tmp_path / "daemon.json"
    write_daemon_state(
        {
            "running": False,
            "events": events,
            "models": {},
            "pool_health": {},
            "history": [],
        },
        state,
    )
    return state


@pytest.mark.asyncio
async def test_export_events_jsonl(tmp_path: Path, monkeypatch, capsys) -> None:
    from opencode_rate_limiter import Config, cmd_check

    events = [
        {"ts": "2026-09-14T00:00:00Z", "kind": "cooldown_armed", "model": "m1", "seconds": 60},
        {"ts": "2026-09-14T00:01:00Z", "kind": "rotation", "from": "a", "reason": "x"},
    ]
    state = _write_state(tmp_path, events)
    monkeypatch.setattr("opencode_rate_limiter.daemon._state.get_daemon_state_path", lambda: state)
    out = tmp_path / "events.jsonl"
    rc = await cmd_check(Config(), _check_ns(export_events=str(out)))
    assert rc == 0
    rows = _read_json_lines(out)
    assert rows == events
    assert "2 events" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_export_events_csv(tmp_path: Path, monkeypatch, capsys) -> None:
    import csv

    from opencode_rate_limiter import Config, cmd_check

    events = [
        {"ts": "2026-09-14T00:00:00Z", "kind": "cooldown_armed", "model": "m1", "seconds": 60},
        {"ts": "2026-09-14T00:01:00Z", "kind": "rotation", "from": "a"},
    ]
    state = _write_state(tmp_path, events)
    monkeypatch.setattr("opencode_rate_limiter.daemon._state.get_daemon_state_path", lambda: state)
    out = tmp_path / "events.csv"
    rc = await cmd_check(Config(), _check_ns(export_events=str(out), export_format="csv"))
    assert rc == 0
    with open(out, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["kind"] for r in rows] == ["cooldown_armed", "rotation"]
    assert rows[0]["model"] == "m1"
    assert "2 events" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_export_events_without_state_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    from opencode_rate_limiter import Config, cmd_check

    monkeypatch.setattr(
        "opencode_rate_limiter.daemon._state.get_daemon_state_path",
        lambda: tmp_path / "missing.json",
    )
    rc = await cmd_check(Config(), _check_ns(export_events=str(tmp_path / "e.jsonl")))
    assert rc == 1
    assert "no daemon state" in capsys.readouterr().out.lower()


@pytest.mark.asyncio
async def test_export_events_rejects_bad_format(tmp_path: Path, capsys) -> None:
    from opencode_rate_limiter import Config, cmd_check

    rc = await cmd_check(
        Config(), _check_ns(export_events=str(tmp_path / "e.txt"), export_format="xml")
    )
    assert rc == 2
    assert "export-format" in capsys.readouterr().out
