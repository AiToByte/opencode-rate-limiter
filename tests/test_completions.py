"""Shell completion generation tests - Phase 5."""

import argparse

import pytest

from opencode_rate_limiter import (
    FREE_MODELS,
    Config,
    cmd_completions,
    generate_completions,
    should_print_banner,
)

ALL_COMMANDS = [
    "quick",
    "deep",
    "probe",
    "headers",
    "rotate",
    "check",
    "daemon",
    "generate-systemd",
    "generate-launchd",
    "generate-task",
    "generate-config",
    "completions",
]


def test_bash_completion_contains_all_commands() -> None:
    script = generate_completions("bash")
    for cmd in ALL_COMMANDS:
        assert cmd in script


def test_bash_completion_contains_models() -> None:
    script = generate_completions("bash")
    assert FREE_MODELS[0] in script
    assert "all" in script


def test_bash_completion_contains_flags_and_strategies() -> None:
    script = generate_completions("bash")
    assert "--interval --models" in script
    assert "round_robin least_used health" in script
    assert "complete -F _opencode_rate_limiter opencode-rate-limiter" in script


def test_zsh_completion_contains_commands_and_help() -> None:
    script = generate_completions("zsh")
    for cmd in ALL_COMMANDS:
        assert cmd in script
    assert "#compdef opencode-rate-limiter" in script
    assert "daemon:后台守护进程模式" in script


def test_zsh_completion_contains_models_and_strategies() -> None:
    script = generate_completions("zsh")
    for model in FREE_MODELS:
        assert model in script
    for strategy in ("round_robin", "least_used", "health"):
        assert strategy in script


def test_fish_completion_contains_commands() -> None:
    script = generate_completions("fish")
    for cmd in ALL_COMMANDS:
        assert cmd in script
    assert "__fish_opencode_rate_limiter_commands" in script


def test_fish_completion_contains_subcommand_options() -> None:
    script = generate_completions("fish")
    assert "__fish_seen_subcommand_from daemon" in script
    assert "-l interval" in script
    assert "-l strategy" in script
    assert "(__fish_opencode_rate_limiter_strategies)" in script


def test_generate_completions_invalid_shell_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported shell"):
        generate_completions("powershell")


def test_generate_completions_all_outputs_differ() -> None:
    from opencode_rate_limiter import _completion_payload

    payload = _completion_payload()
    assert payload["prog"] == "opencode-rate-limiter"
    assert {c["name"] for c in payload["commands"]} == set(ALL_COMMANDS)


@pytest.mark.parametrize(
    ("command", "json_output", "expected"),
    [
        ("quick", False, True),
        ("deep", False, True),
        ("rotate", False, True),
        ("daemon", False, True),
        ("check", False, False),
        ("probe", False, False),
        ("headers", False, False),
        ("generate-task", False, False),
        ("completions", False, False),
        ("quick", True, False),
    ],
)
def test_should_print_banner(command: str, json_output: bool, expected: bool) -> None:
    assert should_print_banner(command, json_output) is expected


@pytest.mark.asyncio
async def test_cmd_completions_prints_script_and_returns_zero(capsys) -> None:
    args = argparse.Namespace(shell="bash", json=False, dry_run=False)
    result = await cmd_completions(Config(), args)
    assert result == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("# opencode-rate-limiter bash completion")
    assert "complete -F _opencode_rate_limiter opencode-rate-limiter" in captured.out
    for cmd in ALL_COMMANDS:
        assert cmd in captured.out
