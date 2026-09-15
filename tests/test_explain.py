"""Explain (offline classification) command tests: zero quota, no network."""

import argparse
import json

import pytest

from opencode_rate_limiter import Config
from opencode_rate_limiter.cli import _explain_one, cmd_diagnose, cmd_explain


def _args(**kwargs: object) -> argparse.Namespace:
    base: dict[str, object] = {"text": None, "from_log": None, "from_text": None, "json": False}
    base.update(kwargs)
    return argparse.Namespace(**base)


_REASONING_LINE = (
    "Upstream request failed: [invalid_request_error] reasoning "
    "`encrypted_content` was not issued to this caller"
)


class TestExplainOne:
    def test_three_real_lines(self):
        e1 = "Cannot connect to API: The socket connection was closed unexpectedly."
        e2 = "Error from provider (Console): Rate limit exceeded. Please try again later."
        e3 = _REASONING_LINE
        assert _explain_one(e1)["kind"] == "transient_transport"
        assert _explain_one(e2)["kind"] == "rate_limited"
        assert _explain_one(e3)["kind"] == "reasoning_replay"
        for e in (e1, e2, e3):
            r = _explain_one(e)
            assert r["title"] and r["remedy"]


class TestCmdExplain:
    @pytest.mark.asyncio
    async def test_single_text_human(self, capsys):
        code = await cmd_explain(Config(), _args(text="Rate limit exceeded. Please try again"))
        assert code == 1
        assert "限额" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_single_text_json(self, capsys):
        code = await cmd_explain(
            Config(), _args(text="socket connection was closed unexpectedly", json=True)
        )
        assert code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["kind"] == "transient_transport"

    @pytest.mark.asyncio
    async def test_from_log_file(self, tmp_path, capsys):
        log = tmp_path / "opencode.log"
        log.write_text(
            "Rate limit exceeded. Please try again later.\n"
            "Upstream request failed: reasoning `encrypted_content` was not issued\n",
            encoding="utf-8",
        )
        code = await cmd_explain(Config(), _args(from_log=log, json=True))
        assert code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["summary"]["rate_limited"] == 1
        assert payload["summary"]["reasoning_replay"] == 1

    @pytest.mark.asyncio
    async def test_no_input_usage(self, capsys):
        assert await cmd_explain(Config(), _args()) == 2


class TestDiagnoseOffline:
    @pytest.mark.asyncio
    async def test_from_text_rate_limited(self, capsys):
        code = await cmd_diagnose(
            Config(), _args(from_text="Rate limit exceeded", json=True, model=None)
        )
        assert code == 1
        assert json.loads(capsys.readouterr().out)["verdict"] == "rate_limited"

    @pytest.mark.asyncio
    async def test_from_text_reasoning(self, capsys):
        code = await cmd_diagnose(
            Config(), _args(from_text="encrypted_content was not issued", json=True, model=None)
        )
        assert code == 2
