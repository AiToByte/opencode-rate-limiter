"""Shell completion script generation derived from the live CLI parser."""

from __future__ import annotations

import argparse
from typing import Any

from .config import FREE_MODELS
from .parser import _GLOBAL_FLAGS, build_parser


def _completion_payload() -> dict[str, Any]:
    """Derive completion data from the live CLI parser (single source of truth)."""
    parser = build_parser()
    commands: list[dict[str, str]] = []
    global_options: list[str] = []
    sub_options: dict[str, list[str]] = {}
    choice_opt: dict[str, list[str]] = {}
    positional_choices: dict[str, list[str]] = {}

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name in sorted(action.choices):
                sub = action.choices[name]
                commands.append({"name": name, "help": sub.description or ""})
                opts: list[str] = []
                for a in sub._actions:
                    if not a.option_strings:
                        if a.dest == "model":
                            positional_choices[name] = [*FREE_MODELS, "all"]
                        continue
                    primary = a.option_strings[0]
                    if primary in _GLOBAL_FLAGS:
                        continue
                    opts.append(primary)
                    if a.choices:
                        choice_opt[primary] = [str(c) for c in a.choices]
                sub_options[name] = opts
        else:
            for flag in action.option_strings:
                if flag not in global_options:
                    global_options.append(flag)

    return {
        "prog": parser.prog,
        "commands": commands,
        "global_options": global_options,
        "sub_options": sub_options,
        "choice_opt": choice_opt,
        "positional_choices": positional_choices,
    }


def _bash_completion(payload: dict[str, Any]) -> str:
    prog = payload["prog"]
    func = prog.replace("-", "_")
    commands = " ".join(c["name"] for c in payload["commands"])
    globals_flags = " ".join(payload["global_options"])
    opts_word = " ".join([commands, globals_flags])

    case_lines = [
        '        --config|--log-file) COMPREPLY=( $(compgen -f -- "${cur}") ); return 0 ;;'
    ]
    for flag in sorted(payload["choice_opt"]):
        choices = " ".join(payload["choice_opt"][flag])
        case_lines.append(
            '        %s) COMPREPLY=( $(compgen -W "%s" -- "${cur}") ); return 0 ;;'
            % (flag, choices)
        )
    for name in sorted(payload["sub_options"]):
        opts = payload["sub_options"][name]
        if opts:
            case_lines.append(
                '        %s) COMPREPLY=( $(compgen -W "%s" -- "${cur}") ); return 0 ;;'
                % (name, " ".join(opts))
            )
    models = " ".join(payload["positional_choices"].get("probe", []))
    if models:
        case_lines.append(
            '        probe) COMPREPLY=( $(compgen -W "%s" -- "${cur}") ); return 0 ;;' % models
        )

    return """# %(prog)s bash completion
_%(func)s() {
    local cur prev
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    case "${prev}" in
%(case)s
    esac

    COMPREPLY=( $(compgen -W "%(opts)s" -- "${cur}") )
    return 0
}
complete -F _%(func)s %(prog)s
""" % {
        "prog": prog,
        "func": func,
        "case": "\n".join(case_lines),
        "opts": opts_word,
    }


def _zsh_completion(payload: dict[str, Any]) -> str:
    prog = payload["prog"]
    func = prog.replace("-", "_")

    commands_block = "\n".join(
        "        '%s:%s'" % (c["name"], c["help"]) for c in payload["commands"]
    )
    options_block = "\n".join("        '%s'" % f for f in payload["global_options"])
    models = payload["positional_choices"].get("probe", [])
    models_block = "\n".join("        '%s'" % m for m in models)
    strategies = payload["choice_opt"].get("--strategy", [])
    strategies_block = "\n".join("        '%s'" % s for s in strategies)

    subargs: list[str] = []
    if "--strategy" in payload["choice_opt"]:
        subargs.append("        '--strategy: :($strategies)'")
    for name in sorted(payload["sub_options"]):
        opts = payload["sub_options"][name]
        if opts:
            subargs.append("        '%s: :(%s)'" % (name, " ".join(opts)))
    if models:
        subargs.append("        'probe: :($probe_models)'")

    return """# %(prog)s zsh completion
#compdef %(prog)s

_%(func)s() {
    local -a commands options probe_models strategies

    commands=(
%(commands)s
    )

    options=(
%(options)s
    )

    probe_models=(
%(models)s
    )

    strategies=(
%(strategies)s
    )

    _arguments -C \\
        ${options} \\
        '(-)'{${commands}} \\
%(subargs)s
}

_%(func)s "$@"
""" % {
        "prog": prog,
        "func": func,
        "commands": commands_block,
        "options": options_block,
        "models": models_block,
        "strategies": strategies_block,
        "subargs": "\n".join(subargs),
    }


def _fish_completion(payload: dict[str, Any]) -> str:
    prog = payload["prog"]
    func = prog.replace("-", "_")

    commands_block = "\n".join("        %s" % c["name"] for c in payload["commands"])
    models = payload["positional_choices"].get("probe", [])
    models_block = "\n".join("        %s" % m for m in models)
    strategies_line = " ".join(payload["choice_opt"].get("--strategy", []))

    extra: list[str] = []
    for name in sorted(payload["sub_options"]):
        opts = [o for o in payload["sub_options"][name] if o not in payload["choice_opt"]]
        if not opts:
            continue
        extra.append('complete -c %s -f -n "__fish_seen_subcommand_from %s" \\' % (prog, name))
        for opt in opts:
            extra.append("    -l %s" % opt.lstrip("-"))
    if "--strategy" in payload["choice_opt"]:
        extra.append(
            'complete -c %s -f -n "__fish_seen_subcommand_from rotate" -l strategy '
            '-a "(__fish_%s_strategies)"' % (prog, func)
        )

    global_lines = "\n".join(
        "    -l %s \\" % flag.lstrip("-")
        for flag in sorted(payload["global_options"])
        if flag.startswith("--") and flag not in ("--help",)
    )

    return """# %(prog)s fish completion
function __fish_%(func)s_commands
    set -l commands \\
%(commands)s
    for cmd in $commands
        echo $cmd
    end
end

function __fish_%(func)s_probe_models
    set -l models \\
%(models)s
    for model in $models
        echo $model
    end
end

function __fish_%(func)s_strategies
    set -l strategies %(strategies)s
    for s in $strategies
        echo $s
    end
end

complete -c %(prog)s -f -n "__fish_use_subcommand" \\
    -a "(__fish_%(func)s_commands)"

complete -c %(prog)s -f -n "__fish_seen_subcommand_from probe" \\
    -a "(__fish_%(func)s_probe_models)"

%(extra)s

complete -c %(prog)s -f \\
%(globals)s
""" % {
        "prog": prog,
        "func": func,
        "commands": commands_block,
        "models": models_block,
        "strategies": strategies_line,
        "extra": "\n".join(extra),
        "globals": global_lines,
    }


def _powershell_completion(payload: dict[str, Any]) -> str:
    """Native PowerShell argument completer (Register-ArgumentCompleter)."""
    prog = payload["prog"]
    commands = "\n".join('        "%s"' % c["name"] for c in payload["commands"])
    models = "\n".join('        "%s"' % m for m in payload["positional_choices"].get("probe", []))
    strategies = " ".join('"%s"' % s for s in payload["choice_opt"].get("--strategy", []))
    sub_cases = []
    for name in sorted(payload["sub_options"]):
        opts = payload["sub_options"][name]
        if not opts:
            continue
        quoted = " ".join('"%s"' % o for o in opts)
        sub_cases.append('        "%s" { @(%s) }' % (name, quoted))
    return """# %(prog)s PowerShell completion
# Install: add this file to your $PROFILE, e.g.
#   opencode-rate-limiter completions powershell >> $PROFILE
Register-ArgumentCompleter -Native -CommandName @('%(prog)s') -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $commands = @(
%(commands)s
    )
    $probeModels = @(
%(models)s
    )
    $strategies = @(%(strategies)s)
    $elements = $commandAst.ToString() -split '\\s+'
    $sub = $elements | Where-Object { $_ -in $commands } | Select-Object -First 1
    if (-not $sub) {
        $commands | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
            [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
        }
        return
    }
    $candidates = switch ($sub) {
%(cases)s
        default { @() }
    }
    if ($sub -eq 'probe') { $candidates += $probeModels }
    if ($elements -contains '--strategy') { $candidates += $strategies }
    $candidates | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }
}
""" % {
        "prog": prog,
        "commands": commands,
        "models": models,
        "strategies": (" " + strategies) if strategies else "",
        "cases": "\n".join(sub_cases),
    }


def generate_completions(shell: str) -> str:
    """Generate a shell completion script (bash / zsh / fish / powershell)."""
    payload = _completion_payload()
    if shell == "bash":
        return _bash_completion(payload)
    if shell == "zsh":
        return _zsh_completion(payload)
    if shell == "fish":
        return _fish_completion(payload)
    if shell == "powershell":
        return _powershell_completion(payload)
    raise ValueError(f"Unsupported shell: {shell}")
