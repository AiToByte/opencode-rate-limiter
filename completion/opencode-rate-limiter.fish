# opencode-rate-limiter fish completion
function __fish_opencode_rate_limiter_commands
    set -l commands \
        check
        completions
        daemon
        deep
        diagnose
        explain
        generate-config
        generate-launchd
        generate-systemd
        generate-task
        headers
        probe
        quick
        rotate
    for cmd in $commands
        echo $cmd
    end
end

function __fish_opencode_rate_limiter_probe_models
    set -l models \
        deepseek-v4-flash-free
        nemotron-3-ultra-free
        big-pickle
        mimo-v2.5-free
        hy3-free
        laguna-s-2.1-free
        ling-3.0-flash-fin-free
        nemotron-3.5-lightning-free
        all
    for model in $models
        echo $model
    end
end

function __fish_opencode_rate_limiter_strategies
    set -l strategies round_robin least_used health
    for s in $strategies
        echo $s
    end
end

complete -c opencode-rate-limiter -f -n "__fish_use_subcommand" \
    -a "(__fish_opencode_rate_limiter_commands)"

complete -c opencode-rate-limiter -f -n "__fish_seen_subcommand_from probe" \
    -a "(__fish_opencode_rate_limiter_probe_models)"

complete -c opencode-rate-limiter -f -n "__fish_seen_subcommand_from check" \
    -l trend
    -l export-events
complete -c opencode-rate-limiter -f -n "__fish_seen_subcommand_from daemon" \
    -l interval
    -l models
    -l once
    -l stop
complete -c opencode-rate-limiter -f -n "__fish_seen_subcommand_from diagnose" \
    -l model
    -l from-text
    -l from-log
complete -c opencode-rate-limiter -f -n "__fish_seen_subcommand_from explain" \
    -l from-log
complete -c opencode-rate-limiter -f -n "__fish_seen_subcommand_from generate-config" \
    -l force
complete -c opencode-rate-limiter -f -n "__fish_seen_subcommand_from headers" \
    -l model
    -l export
complete -c opencode-rate-limiter -f -n "__fish_seen_subcommand_from rotate" \
    -l to
    -l apply
complete -c opencode-rate-limiter -f -n "__fish_seen_subcommand_from rotate" -l strategy -a "(__fish_opencode_rate_limiter_strategies)"

complete -c opencode-rate-limiter -f \
    -l config \
    -l dry-run \
    -l json \
    -l json-verbose \
    -l log-file \
    -l quiet \
    -l verbose \
    -l version \
