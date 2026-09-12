# Changelog

All notable changes to opencode-rate-limiter will be documented in this file.

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Fixed

- **PyInstaller 构建脚本适配包结构**：`scripts/build_binary.py` 仍指向已删除的单文件
  `opencode_rate_limiter.py`，包化后构建直接失败（CI `build-binary` 作业同样会挂）。
  现改为生成入口 shim（`build/_pyinstaller_entry.py`）+ `--paths 项目根`；
  实测构建通过（13.0 MB），并以回归测试守住该入口约定。
- **JSON 日志时间戳为真 UTC**：原实现用 `formatTime` 取本地时间却拼接 `Z` 后缀，
  日志时间与实际相差时区偏移；现用 `record.created` + UTC 显式格式化，并剔除
  `taskName` 等新增 stdlib 键（含测试）。

### Changed

- **代码审查清理**：`probe_all` 空模型列表提前返回（不再空建 AsyncClient）；
  prober 的 httpx 类型从 `Any` 收紧为 `httpx.AsyncClient`（模块级导入）；
  `cmd_probe` 以类型收窄移除 `type: ignore`；路径去重的三处相同循环提取为
  `paths._dedupe`；daemon 内散落的 `import time`/`import signal` 收敛到模块级；
  coverage 统计范围限定为包本身；删除游离的 `test_config.toml`。

- **每模型限流冷却期**：模型探测到 429 后按 `Retry-After`（缺失用 60s 估算）进入冷却，
  冷却期内 daemon 跳过该模型的探测——不再对着已知限流的模型空烧配额；
  恢复可用即解除，`[daemon].respect_cooldown` 可关闭；剩余冷却随状态文件持久化
  并在 `check` 人读输出中显示（跨重启不保留）。
- **`probe` 输出标注账号**：人读输出追加 `[account: <名>]` 标签，JSON 输出每项新增
  `account` 字段，轮换是否生效一目了然。

### Changed

- **429 自动清理每轮去重**：此前同轮每个 rate_limited 模型都会各触发一次
  `full_cleanup()`（一轮最多 N 次全量清理）；现在每轮至多一次。
- `_handle_rate_limited` 只负责失败标记与账号轮换，清理职责上收到探测周期。

- **探测历史环形缓冲**：`[daemon].history_size`（默认 20）条最近探测摘要
  （`{ts, models}`）随状态文件持久化；`check` 人读输出新增
  `probe history` 趋势段（各模型近 N 轮的状态计数）。
- **健康评分权重可配置**：`[account_pool].score_weights`
  （默认 `success=0.5, latency=0.3, recency=0.2`），校验三键齐全、值域 [0,1]、
  和为 1；`health` 策略与 `pool_health` 快照均使用配置权重。
- **`[prober].http2` / `[prober].connection_pool_size`**：可选 HTTP/2（需安装
  `h2`，新增 `http2` optional-dependency 组，缺失时告警回退）与共享连接池大小。

### Changed

- **探测共享 AsyncClient**：一轮 `probe_all` 复用同一个连接池化的
  `httpx.AsyncClient`（此前每个探测各建一个客户端），降低每轮的连接握手开销；
  单独 `probe()` 仍用一次性客户端，`_shared_client` 用后即清。
- 偶发脆弱的并发计时测试放宽时间边界（sleep 0.05→0.2、断言 0.12s→0.35s）。

- **`check` 人读输出**：默认输出摘要（版本、守护进程配置与运行时、账号池与健康度、
  最近探测结果、路径计数），`--json` 才输出完整 JSON 报告，`--json` 选项自此有意义。
- **显式构建后端**：`pyproject.toml` 增加 hatchling `[build-system]` 与 wheel 打包配置，
  包化后 `pip install .` / 构建行为有明确定义。

### Changed

- **架构重构：单文件拆分为包**（~2500 行 → 13 个职责单一模块）。依赖单向：
  `cli` → `parser` / `daemon` / `completions` / …；`__init__.py` 统一再导出公共 API，
  `from opencode_rate_limiter import ...` 与入口点 `opencode_rate_limiter:main` 保持不变。
  模块划分：`meta` / `paths` / `config` / `headers` / `prober` / `pool` / `cleanup` /
  `logs` / `daemon` / `service` / `parser` / `completions` / `cli`。
- 测试中对内部函数的 monkeypatch 目标随模块划分更新（patch 使用方模块）。

## [0.2.0] - 2026-09-12

### Added

- **`rotate --apply`：真实切换账号**：把选中账号解析出的 auth JSON 写入活动的
  OpenCode `auth.json`（目标已存在时先备份为 `auth.json.bak`，与清理器备份约定一致）；
  账号无可解析 auth 时报错退出码 1；`--dry-run` 预览目标路径不写盘。
  JSON 输出新增 `applied`（applied/dry_run/no_resolvable_auth）与 `auth_target` 字段。
- **daemon 重载保留健康度**：SIGHUP 触发 `_reload_config()` 重建组件时，同名账号的
  `AccountHealth`（含滑动窗口）跨重载保留，策略决策与 `pool_health` 快照不再归零。

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