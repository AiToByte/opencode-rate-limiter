# opencode-rate-limiter zsh completion
#compdef opencode-rate-limiter

_opencode_rate_limiter() {
    local -a commands options probe_models strategies

    commands=(
        'check:健康检查聚合输出'
        'completions:生成 shell 补全脚本 (bash/zsh/fish)'
        'daemon:后台守护进程模式'
        'deep:深度清理（+ 清除缓存 + 强制重新登录）'
        'generate-config:生成默认配置文件（已存在时需 --force 覆盖）'
        'generate-launchd:生成 launchd plist'
        'generate-systemd:生成 systemd 服务文件'
        'generate-task:生成 Windows 任务计划 XML'
        'headers:输出官方 CLI 兼容请求头'
        'probe:探测模型可用性'
        'quick:快速解除限流（清理退避锁 + 重置 Token）'
        'rotate:手动轮换账号池'
    )

    options=(
        '-h'
        '--help'
        '--config'
        '--json'
        '-v'
        '--verbose'
        '-q'
        '--quiet'
        '--dry-run'
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
        'daemon: :(--interval --models)'
        'generate-config: :(--force)'
        'headers: :(--model --export)'
        'rotate: :(--strategy)'
        'probe: :($probe_models)'
}

_opencode_rate_limiter "$@"
