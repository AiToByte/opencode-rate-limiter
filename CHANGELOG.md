# Changelog

All notable changes to opencode-rate-limiter will be documented in this file.

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- **`[prober]` 配置段**：探测端点（`endpoint`）、ping 载荷（`ping_message`/`max_tokens`）、
  附加请求头（`extra_headers`）与代理（`proxy`，httpx ≥ 0.28）均可配置，
  环境变量 `OPENCODE_RATE_LIMITER_PROBER__*` 同样生效；`Config.save()` /
  `generate-config` 会带上该段（`proxy` 为空时不写入，规避 tomli_w 的 None 限制）。
- **健康度滑动窗口**：`AccountHealth` 记录最近 N 次探测结果（默认 100，可经
  `[account_pool].health_window` 配置），`success_rate` 优先取窗口值，窗口为空时回退
  生命周期累计计数；`health` 策略因此更能反映近期表现。
- **账号健康度持久化**：daemon 每个探测周期把账号健康快照（success/total/
  consecutive_failures/avg_latency/score）写入 `daemon.json` 的 `pool_health` 字段；
  `check` 将其归位到输出的 `account_pool.health`。
- **daemon 连续错误退避**：整轮探测全部 `error` 时错误连击 +1，等待间隔按
  1×→2×→4×→8×（上限 8×）指数放大，任一探测恢复正常即复位。
- **`OPENCODE_VERSION` 环境变量**：可覆盖版本探测（容器内或 opencode 不在 PATH 时有用）。
- **Windows 控制台编码防护**：win32 下对 stdout/stderr `reconfigure(errors="replace")`，
  GBK 控制台不再因个别字符抛 UnicodeEncodeError。

- **账号轮换接入真实请求**：`probe` 与 `daemon` 现在按账号轮换为每次探测注入
  `Authorization: Bearer <token>`（token 由 `AccountPool.read_auth` +
  `extract_access_token` 解析，支持顶层与一层嵌套的 `access_token`），探测结果
  （available/rate_limited）回写账号健康度，`health`/`least_used` 策略自此有真实数据。
  token 无法解析时回退为无鉴权头（原行为）。
- **`OPENCODE_RATE_LIMITER_CONFIG` 环境变量生效**：`Config.load()` 的配置文件路径
  解析优先级为 CLI `--config` > 该环境变量（支持 `~`/`$VAR`/`%VAR%` 展开）> 平台默认
  目录，生成的服务文件中的该变量从此有效。
- **`quick` / `deep` 行为拆分**：`quick` = 清理限流锁 + `state.json` + 重置 token
  （不再清缓存）；`deep` = quick 全部 + 清空缓存，并在输出中附带 `opencode login`
  重新登录提示（JSON 输出新增 `relogin_hint` 字段）。
- **`generate-config` 子命令**：把内置默认配置写入平台配置目录（或 `--config` 指定
  路径），已存在时需 `--force` 覆盖；`Config.save()` 从此接入 CLI。
- **守护进程单实例锁**：`daemon` 启动时在状态目录创建 `daemon.lock`（记录 pid），
  检测到存活实例则报错退出（退出码 1）；死进程遗留的过期锁自动接管。Windows 下
  存活检测走 `OpenProcess`（避免 `os.kill` 误杀进程）。
- **`rotate` 输出增强**：JSON 输出新增 `auth_token_resolved` 与 `dry_run` 字段；
  人类可读输出显示所选账号 token 是否可解析，`--dry-run` 不再被静默忽略。

- **Phase 5 — Shell 补全生成**：新增 `completions bash|zsh|fish` 子命令，补全内容
  由真实 CLI parser 派生（子命令、参数、`--strategy` 选项、免费模型列表单一数据源）。
  `scripts/generate_completions.py` 改为委托主模块，一键写入 `completion/`。
- **模块级 `FREE_MODELS` 常量**：`DaemonConfig` 默认模型与补全共用同一数据源。
- **结构化输出净化**：`should_print_banner()` 使 `check`/`probe`/`headers`/
  `generate-*`/`completions` 命令及所有 `--json` 输出不再打印启动横幅，
  保证 `check --json | jq` 等管道场景输出纯净。
- **Windows/Linux/macOS 构建脚本完善**：`scripts/build_binary.py` 平台标签命名、
  `--strip` 仅 Unix 启用、移除多余 `--uac-admin`/`--icon NONE`、GBK 控制台
  `[OK]`/`[FAIL]` 安全输出、根目录 `*.spec` 清理。
- **新测试文件**：`tests/test_completions.py`（20 项）、`tests/test_build_binary.py`
  （动态加载构建脚本，避免真实执行 PyInstaller）。

### Changed

- `build_parser()` 改用 `add_sub` 闭包注册子命令，`help` 自动镜像为 `description`，
  使 `X -h` 与补全都能读取子命令单行摘要。
- CI `build-binary`/`release` 作业改用动态探测二进制文件名，与真实产物对齐。
- 移除 pre-commit 中未使用的 `types-requests` 依赖。

### Fixed

- `tests/test_daemon.py::test_periodic_probe_execution` 在新版 pytest-httpx 下因注册
  响应被单次消费而报 error（改用 fake probe，不再依赖 mock 响应复用行为）。
- `--json`/`--dry-run` 在子命令后使用时丢失（改用带 `argparse.SUPPRESS` 默认值的
  共享父 parser，前后位置均可）。
- 守护进程 `--interval` 默认值错误地覆盖配置文件（改为 `None` 保留配置值）。
- Banner 污染 JSON/XML 管道输出的问题。

### 早期积累（Phase 0-4）

- 配置管理、跨平台路径解析、结构化日志
- 头部注入器、模型探测（P95 端点）、账号池轮换、退避锁/缓存清理
- 守护进程模式（周期探测、429 自动清理与账号轮换、UNIX 信号、状态持久化）
- `generate-systemd` / `generate-launchd` / `generate-task` 服务文件生成