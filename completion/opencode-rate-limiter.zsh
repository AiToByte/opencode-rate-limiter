# opencode-rate-limiter zsh completion
#compdef opencode-rate-limiter

_opencode_rate_limiter() {
    local -a commands options probe_models strategies

    commands=(
        'check:健康检查聚合输出'
        'completions:生成 shell 补全脚本 (bash/zsh/fish/powershell)'
        'daemon:后台守护进程模式'
        'deep:深度维护：quick + 清缓存'
        'diagnose:Zen 限额诊断（出口 IP / 代理 / 错误层级 / 建议）'
        'generate-config:生成默认配置文件（已存在时需 --force 覆盖）'
        'generate-launchd:生成 launchd plist'
        'generate-systemd:生成 systemd 服务文件'
        'generate-task:生成 Windows 任务计划 XML'
        'headers:输出官方 CLI 兼容请求头'
        'probe:探测模型可用性'
        'quick:快速维护：备份 auth.json（本地操作不解除服务端限额）'
        'rotate:手动轮换账号池'
    )

    options=(
        '-h'
        '--help'
        '--config'
        '--json'
        '--json-verbose'
        '-v'
        '--verbose'
        '-q'
        '--quiet'
        '--dry-run'
        '--log-file'
        '--version'
    )

    probe_models=(
        'deepseek-v4-flash-free'
        'nemotron-3-ultra-free'
        'big-pickle'
        'mimo-v2.5-free'
        'hy3-free'
        'laguna-s-2.1-free'
        'ling-3.0-flash-fin-free'
        'nemotron-3.5-lightning-free'
        'all'
    )

    strategies=(
        'round_robin'
        'least_used'
        'health'
    )

    _arguments -C \
        ${options} \
        '(-)'{${commands}} \
        '--strategy: :($strategies)'
        'check: :(--trend --export-events --export-format)'
        'daemon: :(--interval --models --once --stop)'
        'diagnose: :(--model)'
        'generate-config: :(--force)'
        'headers: :(--model --export)'
        'rotate: :(--strategy --to --apply)'
        'probe: :($probe_models)'
}

_opencode_rate_limiter "$@"
