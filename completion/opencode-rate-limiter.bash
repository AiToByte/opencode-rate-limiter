# opencode-rate-limiter bash completion
_opencode_rate_limiter() {
    local cur prev
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    case "${prev}" in
        --config) COMPREPLY=( $(compgen -f -- "${cur}") ); return 0 ;;
        --strategy) COMPREPLY=( $(compgen -W "round_robin least_used health" -- "${cur}") ); return 0 ;;
        check) COMPREPLY=( $(compgen -W "--trend" -- "${cur}") ); return 0 ;;
        daemon) COMPREPLY=( $(compgen -W "--interval --models" -- "${cur}") ); return 0 ;;
        diagnose) COMPREPLY=( $(compgen -W "--model" -- "${cur}") ); return 0 ;;
        generate-config) COMPREPLY=( $(compgen -W "--force" -- "${cur}") ); return 0 ;;
        headers) COMPREPLY=( $(compgen -W "--model --export" -- "${cur}") ); return 0 ;;
        rotate) COMPREPLY=( $(compgen -W "--strategy --apply" -- "${cur}") ); return 0 ;;
        probe) COMPREPLY=( $(compgen -W "deepseek-v4-flash-free nemotron-3-ultra-free big-pickle mimo-v2.5-free hy3-free laguna-s-2.1-free ling-3.0-flash-fin-free nemotron-3.5-lightning-free all" -- "${cur}") ); return 0 ;;
    esac

    COMPREPLY=( $(compgen -W "check completions daemon deep diagnose generate-config generate-launchd generate-systemd generate-task headers probe quick rotate -h --help --config --json -v --verbose -q --quiet --dry-run --version" -- "${cur}") )
    return 0
}
complete -F _opencode_rate_limiter opencode-rate-limiter
