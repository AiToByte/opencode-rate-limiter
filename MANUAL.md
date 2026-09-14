# opencode-rate-limiter 技术说明与使用手册

版本：0.6.1
适用范围：本手册内容全部来自对 `opencode_rate_limiter/` 包实际代码的核对，不描述任何未实现的功能。场景化操作见 `docs/user-guide.md`，架构/实现/功能文档见
`docs/`；早期拆分文档已移除，其历史差异结论保留在文末「实现事实与文档差异」。

---

## 目录

1. [项目概述](#1-项目概述)
2. [技术架构](#2-技术架构)
3. [安装](#3-安装)
4. [命令参考](#4-命令参考)
5. [配置文件规范](#5-配置文件规范)
6. [守护进程模式](#6-守护进程模式)
7. [Shell 补全](#7-shell-补全)
8. [二进制打包](#8-二进制打包)
9. [退出码](#9-退出码)
10. [故障排查](#10-故障排查)
11. [开发与质量保障](#11-开发与质量保障)
12. [实现事实与文档差异](#12-实现事实与文档差异)
13. [已知限制](#13-已知限制)

---

## 1. 项目概述

`opencode-rate-limiter` 是一个用于缓解 OpenCode Zen 免费模型限流（429 / TPM / RPM）的命令
行工具，提供四类能力：

| 能力 | 说明 |
|------|------|
| 头部注入 | 生成与官方 CLI 一致的请求头（User-Agent / x-opencode-client / x-opencode-version），并导出为环境变量或 curl 参数 |
| 模型探测 | 并发向 Zen 端点发送「ping」探测，判断每个免费模型当前是否可用 / 被限流 / 出错 |
| 账号池轮换 | 维护多账号健康度（成功率 / 延迟 / 最近错误），支持三种选取策略 |
| 本地维护 | 备份 auth.json（内容不变）、清空缓存目录。**注意：服务端按 IP/UTC 日计数，本地操作不能解除限额** |

另提供守护进程模式（周期探测 + 429 冷却/预算控制 + 信号控制）和三端服务文件生成
（systemd / launchd / Windows 任务计划），以及 bash/zsh/fish 补全脚本生成。

> **定位说明（2026-09 源码核实）**：Zen 免费层限额在服务端按 IP/UTC 日计数（Redis），
> 本工具**不能解除服务端限额**；它的价值在于探测可观测、冷却与预算控制（不浪费
> 配额）以及多 key（付费/BYOK）场景下的轮换管理。详见附录 A。

### 特性摘要

- **纯 Python 包**（`opencode_rate_limiter/`，13 个职责单一模块），Python 3.11+（使用标准库 `tomllib`）
- 运行时依赖仅三个：`httpx`、`platformdirs`、`tomli-w`（`tomli-w` 仅 `generate-config`
  写配置时使用，可视为可选）
- 配置优先级：CLI `--config` > `$OPENCODE_RATE_LIMITER_CONFIG` 环境变量 > 平台配置目录
  `config.toml` > 内置默认值
- 结构化 JSON 日志（stderr）与命令行友好日志
- 探测并发执行（`asyncio.gather`）
- 结构化输出命令自动隐藏启动横幅，保证管道输出纯净

### 目录结构

```
opencode-rate-limiter/
├── opencode_rate_limiter/     # 包实现（config / paths / prober / pool / cleanup / daemon / cli 等）
├── pyproject.toml             # 依赖、ruff / mypy / pytest 配置、入口点
├── README.md                  # 快速开始
├── MANUAL.md                  # 本手册
├── CHANGELOG.md               # 变更日志（Keep a Changelog 格式）
├── docs/                      # architecture / implementation / features / user-guide
│   ├── architecture.md
│   ├── configuration.md
│   ├── daemon-mode.md
│   ├── troubleshooting.md
│   ├── advanced.md
│   └── release.md
├── scripts/
│   ├── build_binary.py        # PyInstaller 打包脚本
│   └── generate_completions.py# 一键重新生成 completion/ 下的补全脚本
├── completion/                # bash / zsh / fish 补全脚本（已生成）
├── tests/                     # pytest 测试（294 个）
├── man/opencode-rate-limiter.1
└── .github/workflows/ci.yml   # CI（含二进制构建与发布 job）
```

---

## 2. 技术架构

### 2.1 模块总览

包结构按职责分模块组织（v0.2.0 起由单文件重构为包，`opencode_rate_limiter/__init__.py`
统一再导出公共 API）：

| 模块 | 内容 |
|------|------|
| `meta` | `__version__` |
| `paths` | `get_opencode_config_dirs()`、`get_opencode_native_cache_dirs()`、`get_opencode_native_state_files()`、`get_opencode_auth_files()`、`get_opencode_version()`（`OPENCODE_VERSION` 可覆盖） |
| `config` | `FREE_MODELS`、`DaemonConfig`、`AccountPoolConfig`、`ProberConfig`、`HeadersConfig`、`CleanupConfig`、`Config` |
| `headers` | `HeaderInjector` |
| `prober` | `ProbeResult`、`ModelProber` |
| `pool` | `AccountHealth`、`Account`、`extract_credential`、`build_auth_payload`、`AccountPool` |
| `cleanup` | `CleanupResult`、`CleanupManager` |
| `logs` | `JSONFormatter`、`HumanFormatter`、`setup_logging`、`level_from_args` |
| `parser` | `build_parser()`、`should_print_banner()`、结构化命令清单 |
| `completions` | `_completion_payload`、`_bash/_zsh/_fish/_powershell_completion`、`generate_completions` |
| `daemon/` | 包：`_lock`（单实例锁）/`_state`（状态持久化）/`_runner`（`RateLimiterDaemon` 主循环） |
| `render` | `check`/`probe` 人读渲染（trend/events/diff），`cli` 兼容重导出 |
| `service` | `generate_systemd_unit` / `generate_launchd_plist` / `generate_task_xml` |
| `cli` | `cmd_*` 命令处理器、`COMMAND_HANDLERS`、`main()` |

依赖方向：`cli` → `parser`/`daemon`/`completions`/... 单向依赖，无循环
（`completions` 只依赖 `parser` 与 `config`）。

### 2.2 数据流

```
用户调用 CLI
  → build_parser() 解析参数
  → setup_logging()（默认 WARNING；-v→INFO；-vv→DEBUG；-q→ERROR；日志写 stderr）
  → Config.load()（优先级：--config > 平台配置目录 > 默认值，随后 validate + 展开路径）
  → 需校验通过后：若为非结构化命令且非 --json，打印横幅
  → 分发至 COMMAND_HANDLERS[command]
        quick / deep       → CleanupManager.full_cleanup()
        probe              → HeaderInjector + ModelProber.probe_all()（并发）
        headers            → HeaderInjector（JSON 字典 或 export 语句）
        rotate             → AccountPool.get_next()（按策略选择）
        check              → 拼装配置 + 运行时守护状态 → JSON
        daemon             → RateLimiterDaemon.run()（无限循环 + 信号）
        generate-*         → 服务文件模板（不可配置，直接打印）
        completions        → 由真实 parser 派生补全脚本
```

### 2.3 核心类说明

**Config（配置聚合 + 合并 + 校验）**
- 四个子配置对象：`daemon`、`account_pool`、`headers`、`cleanup`，各有默认值。
- 空配置文件不存在时返回默认值；文件 TOML 语法错误时抛出 `ValueError`（导致退出码 2）。
- 合并顺序：文件 → 环境变量 → 覆盖既有值；`_coerce_value` 会把字符串按目标字段类型
  强转（bool / int / float），这是环境变量能覆盖 TOML 数值字段的原因。
- 所有 `OPENCODE_RATE_LIMITER_<SECTION>__<KEY>` 形式的环境变量都会被收集合并。
- 字段级校验（validate）：`daemon.interval_seconds >= 5`、`daemon.probe_timeout_seconds > 0`、
  `models` 非空、`strategy` ∈ {round_robin, least_used, health}、每个 `account` 必须含 `name`
  且至少带一种认证来源、`cleanup.preserve_config` 必须为 `true`。

**HeaderInjector（请求头模板）**
- 模板仅支持 `{version}` 占位符（`.format()` 替换）。`build_headers(model, token)` 输出：

| 头 | 值 |
|----|----|
| `User-Agent` | `config.headers.user_agent`（默认 `opencode/{version}`，`{version}` 为检测到的 opencode 版本） |
| `x-opencode-client` | `config.headers.x_opencode_client`（默认 `opencode-cli`） |
| `x-opencode-version` | `config.headers.x_opencode_version`（默认 `{version}`） |
| `Content-Type` | `application/json` |
| `Accept` | `text/event-stream` |
| `x-model` | 仅在指定 `model` 时追加 |
| `Authorization` | 仅在指定 `token` 时追加，值为 `Bearer <token>` |

- `to_env_export(model)`：把上述头转成 `export KEY="value"` 语句，键名大写、`-` 变 `_`
  （如 `USER_AGENT`、`X_OPENCODE_CLIENT`、`X_OPENCODE_VERSION`、`CONTENT_TYPE`、`ACCEPT`、`X_MODEL`）。
- `to_curl_args(model)`：转成 `-H "Key: value"` 片段（测试用）。

**ModelProber（并发探测）**
- 端点默认 `https://opencode.ai/zen/v1/chat/completions`，可经 `[prober].endpoint` 配置；
  探测请求体 `{model, messages:[{role:"user", content: [prober].ping_message}],
  max_tokens: [prober].max_tokens, temperature:0}`。
- `[prober].extra_headers` 会合并进每次探测请求头（同名键覆盖共享头）；
  `[prober].proxy` 传给 httpx（`proxy=` 参数，需 httpx ≥ 0.28），缺省遵循
  `HTTP_PROXY`/`HTTPS_PROXY` 环境变量。
- 判定：200 → `available`；429 → `rate_limited`（优先读取响应的 `Retry-After` 头——Zen
  网关的 429 确实携带该头，值为距 UTC 午夜的秒数；缺失时 `estimated_reset` 估算为
  距 UTC 午夜的秒数，与网关的重置点一致）；其他状态码 → `error`（`error="HTTP xxx"`）；
  超时 → `error (timeout)`；未知异常 → `error(<异常文本>)`。空模型列表时 `probe_all`
  立即返回空列表（不建立连接）。
- `probe_all(models, headers, headers_by_model)` 支持按模型覆盖请求头——`probe`/`daemon`
  用它实现按账号注入 `Authorization`（见下）。
- **共享连接池**：一轮 `probe_all` 全程复用同一个 `httpx.AsyncClient`
  （连接池大小 `[prober].connection_pool_size`，默认 8；`[prober].http2 = true` 启用
  HTTP/2，需安装可选依赖 `h2`——`pip install 'opencode-rate-limiter[http2]'`，
  缺失时告警并回退 HTTP/1.1）。单独调用的 `probe()` 使用一次性客户端。

**账号轮换与探测的结合（probe / daemon）**
- 配置了账号池时，`probe` 与 daemon 的每个探测周期都会**按策略为每个模型选取一个账号**
  （`get_next()`），用 `resolve_token()` 从其 auth 来源解析 bearer token，成功则该模型的
  探测请求携带 `Authorization: Bearer <token>`；token 解析失败则回退为无鉴权头（原行为）。
- 每个探测结果都会回写账号健康度（`mark_result`，R1 归因规则）：`available` 记
  成功；`FreeUsageLimitError`（IP 配额）不计入凭证健康度；`RateLimitError`
  （key RPM）记 key 维度失败并使该账号进入 60s key 冷却（冷却中不注入凭证，
  全部冷却时回退匿名头）；其他失败照常记失败。`health` / `least_used`
  策略因此能基于真实请求结果演化。

**AccountPool（账号池）**
- 由 `[account_pool]` 下的 `accounts` 列表构造 `Account(name, auth_path, env_var, auth_json[, kind])`；
  `kind` 可显式指定凭证形态标签（默认从 auth 结构推断）。
- 三种策略见 §5；健康度经 `mark_result()` 更新——`probe` 与 daemon 的探测结果都会回写
  （见上文「账号轮换与探测的结合」）；CLI `rotate` 只做选择，不写健康数据。
  **归因规则（R1）**：`FreeUsageLimitError`（IP 配额）不计入凭证健康度；
  `RateLimitError`（key RPM）记为 key 维度失败（`key_limited_count`）。
- `read_auth()` 按 `auth_json` > `env_var` > `auth_path` 的优先级读取 auth JSON
  （`auth_path` 仅做 `~` 展开），任何一步解析失败返回 `None`。
- `resolve_credential(account)` = `read_auth` + `extract_credential`，返回
  `(kind, token)`：kind 为 `oauth`（顶层/一层嵌套的 `access`/`access_token`）或
  `api`（`{"type":"api","key"}`、provider 键控或裸 key 形态）；`resolve_token`
  为其只取 token 的兼容别名。
- `credential_fingerprint(token)`：前 6 后 4 的脱敏展示形式；`rotate`/`check`/
  诊断的凭证信息一律以指纹输出，明文不出现在任何输出中。
- **健康度滑动窗口**：每个账号记录最近 `[account_pool].health_window` 次（默认 100）
  探测结果，`success_rate` 优先取窗口内成功率，窗口为空时回退累计计数；延迟仍为
  指数移动平均（新结果权重 0.2）。

**CleanupManager（auth 备份与缓存维护）**
> 早期版本会删除「限流锁文件 / state.json / auth 内 token 字段」——经 opencode 源码
> 核实，这些目标**并不存在**（opencode CLI 无本地限流状态，auth.json 中也没有
> `access_token`/`rate_limited_until` 字段），相关操作已移除。
- `backup_auth_files()`：把 auth.json 原样备份为 `<同名>.json.bak`，**不修改内容**。
- `purge_cache()`：递归删除缓存目录内容后**重建同名空目录**。
- `full_cleanup(dry_run, include_cache=True)`：备份 +（可选）清缓存并汇总结果；
  `include_cache=False` 即 `quick` 档位。两者均不影响服务端配额。

**RateLimiterDaemon（守护进程）**
- 构造时 `_rebuild()` 组装 injector / prober / cleanup / pool（账号池为空则 `None`）。
- 主循环：`_probe_cycle()`（见 §6 状态机）→ `_wait(interval)`（监听 stop/probe 事件，
  超时后进入下一轮）。
- 状态经 `_persist_state()` 原子写入状态文件（`.tmp` + `replace`），写失败仅记 debug 日志。

### 2.4 免费模型池（`FREE_MODELS`，单数据源）

```
deepseek-v4-flash-free, nemotron-3-ultra-free, big-pickle, mimo-v2.5-free,
hy3-free, laguna-s-2.1-free, ling-3.0-flash-fin-free, nemotron-3.5-lightning-free
```

`FREE_MODELS` 是模块级常量，同时用作 `DaemonConfig.models` 默认值与 `probe` 补全候选
（单一数据源，见 §7）。

---

## 3. 安装

### 3.1 源码 / uv 开发模式

```bash
uv sync --dev          # 安装全部依赖（含开发依赖、lint、类型检查）
uv run pytest          # 运行测试（294 个）
uv run opencode-rate-limiter --help   # 临时运行
```

### 3.2 pip 传统安装（生产）

```bash
pip install .          # 安装 opencode-rate-limiter 到当前 Python 环境
opencode-rate-limiter --version
```

### 3.3 二进制（Windows 示例）

见 §8。产物为单文件可执行程序，无需 Python 环境。构建产物已实测
（`dist/opencode-rate-limiter-windows-amd64.exe`，30.6 MB，onefile）：
`--version`、`completions zsh`、`check --json` 均可正常使用。

### 3.4 环境要求

- Python 3.11+（`tomllib`、`ExceptionGroup`、`TaskGroup`）
- opencode CLI 需在 PATH 中（用于探测真实版本号；缺失时版本回退为 `unknown`；
  可用 `OPENCODE_VERSION` 环境变量手动指定）

---

## 4. 命令参考

### 4.1 全局选项（适用于所有命令，位置可在子命令之前或之后）

| 选项 | 说明 |
|------|------|
| `--config PATH` | 指定配置文件路径 |
| `--json` | 日志 / 部分输出使用 JSON（默认精简字段；`--json-verbose` 附加代码位置） |
| `--json-verbose` | JSON 日志附带 `module`/`function`/`line` 定位字段 |
| `-v, --verbose` | `-vv`→DEBUG（默认已是 INFO，`-v` 保持兼容） |
| `-q, --quiet` | 仅输出 ERROR 级日志 |
| `--dry-run` | 预览模式不实际修改文件（**仅 quick / deep 与部分清理路径生效**，见 §13） |
| `--log-file PATH` | 日志同时追加到文件（1MB×4 轮转，UTF-8；与 stderr 同格式） |
| `--version` | 打印版本号并退出（argparse 内置，退出码 0） |
| `-h, --help` | 帮助 |

说明：
- `--json`、`--dry-run`、`--log-file`、`--json-verbose` 通过共享父 parser（`common`）
  注入所有子命令，因此写在子命令前后都有效（未显式给出时保持父层值——`SUPPRESS` 默认值技巧）。
- 结构化输出命令（`check`、`probe`、`headers`、`generate-systemd`、`generate-launchd`、
  `generate-task`、`completions`）**不打印启动横幅**；其余命令（`quick`、`deep`、`rotate`、
  `daemon`）打印横幅，除非指定 `--json`。
- 从属参数：`probe` 的 `--model`… 见各命令。

### 4.2 `quick` —— 快速维护（auth 备份）

```
opencode-rate-limiter quick [--json] [--dry-run]
```

- 行为：执行 `full_cleanup(include_cache=False)` —— **仅备份 auth.json**（`.json.bak`，
  内容不变）。不清缓存、不清 token。
- 诚实声明：输出会附带配额提示——服务端按 IP/UTC 日计数，本地操作不能解除限流。
- 人类可读输出：逐条打印 `details`（`  <详情>`），错误打印 `  ERROR: <错误>`，末尾
  `Done: <N> items processed, <M> errors`。
- `--json` 输出：`{"cleared_count": N, "errors": [...], "details": [...]}`。
- 退出码：有错误 → `1`，无错误 → `0`。

示例（先预览后执行）：

```bash
opencode-rate-limiter quick --dry-run
opencode-rate-limiter quick --json
```

### 4.3 `deep` —— 深度清理

```
opencode-rate-limiter deep [--json] [--dry-run]
```

- 行为：`quick` 的全部操作 **+ 清空缓存目录**（`full_cleanup(include_cache=True)`）。
  token 不会被清除（早期版本会清空 token 迫使重新登录——该操作已移除，它对服务端
  限额无效且会登出用户）。

### 4.4 `probe` —— 探测模型可用性

```
opencode-rate-limiter probe [MODEL] [--json]
```

| 参数 | 说明 |
|------|------|
| `MODEL` | 模型名；省略或传 `all` 时探测 `[daemon].models` 中所有模型 |

- 行为：并发向 `https://opencode.ai/zen/v1/chat/completions` 发送 `ping` 探测。
  配置了账号池时，按策略为每个模型选取账号并注入其 `Authorization: Bearer <token>`
  头（token 无法解析的账号回退为无鉴权头），探测结果回写账号健康度（见 §2.3）。
- 人类可读输出（每模型两行）：
  ```
    [+] deepseek-v4-flash-free: available (45ms) [account: primary]
    [!] nemotron-3-ultra-free: rate_limited (120ms) [account: backup1]
        retry_after: 60s
    [x] big-pickle: error (2000ms)
        error: timeout
  ```
  配置账号池时，JSON 输出每项额外含 `"account"` 字段（服务该模型的账号，未配置时为 null）。
  图标映射：`available→+`、`rate_limited→!`、`error→x`、`unknown→?`。
- `--json` 输出：`ProbeResult.to_dict()` 数组：
  `model / status / http_status / retry_after / estimated_reset / latency_ms / error / timestamp`。
- 退出码：任意模型 `rate_limited` → `1`，否则 → `0`。

示例：

```bash
opencode-rate-limiter probe                       # 探测全部配置模型
opencode-rate-limiter probe deepseek-v4-flash-free
opencode-rate-limiter probe all --json | jq .
```

### 4.5 `headers` —— 输出官方兼容请求头

```
opencode-rate-limiter headers [--model MODEL] [--export]
```

| 选项 | 说明 |
|------|------|
| `--model MODEL` | 追加 `x-model: MODEL` 请求头 |
| `--export` | 输出 `export KEY="value"` 语句（供 `eval`/`source` 使用） |

- 默认输出：请求头 JSON 字典（缩进 2）。
- `--export` 输出：形如
  ```
  export USER_AGENT="opencode/1.18.30"
  export X_OPENCODE_CLIENT="opencode-cli"
  export X_OPENCODE_VERSION="1.18.30"
  export CONTENT_TYPE="application/json"
  export ACCEPT="text/event-stream"
  ```
- 版本来源：`get_opencode_version()` 运行 `opencode --version`（stdout/stderr 均可），
  失败回退 `unknown`。
- 退出码恒为 `0`。

示例：

```bash
# 直接查看
opencode-rate-limiter headers --json
# 供 curl 使用
eval "$(opencode-rate-limiter headers --export)"
# 指定模型
opencode-rate-limiter headers --model deepseek-v4-flash-free
```

### 4.6 `rotate` —— 手动轮换账号池

```
opencode-rate-limiter rotate [--strategy round_robin|least_used|health] [--to NAME]
```

| 选项 | 默认 | 说明 |
|------|------|------|
| `--strategy` | 配置值 | 选择策略；仅显式传入时**覆盖**配置文件中的 `strategy` |
| `--to NAME` | 无 | 直接选中指定账号（大小写敏感），跳过策略选择；未知名报错退出码 `1` |
| `--apply` | 关 | 把选中账号解析出的 auth JSON 写入活动的 OpenCode `auth.json`（先备份为 `auth.json.bak`） |

- 行为：仅显式传入 `--strategy` 时覆盖配置，否则使用配置文件值；构造 `AccountPool`，调用
  `get_next()` 选出「下一个」账号，并用 `resolve_credential()` 解析其凭证
  （kind + token）。
- **`--apply`**：把选中账号的凭证**归一化**写入目标 `auth.json`
  （第一个已存在的候选 auth 文件，均不存在时创建第一个候选路径；已存在则先备份）。
  归一化规则（`build_auth_payload()`）：api key 写为
  `{zen键: {type:"api", key}}`；单条 oauth / bare 载荷包裹为 provider 键控结构；
  完整 provider 键控快照原样透传。
  账号无可解析凭证时报错，退出码 `1`；与 `--dry-run` 组合只打印目标路径不写盘。
- `--json` 输出：`{"rotated_to": name, "strategy": ..., "explicit": bool, "accounts": [...],
  "auth_token_resolved": bool, "credential": {"kind","fingerprint"}|null,
  "dry_run": bool, "applied": "applied"|"dry_run"|null, "auth_target": path|null}`。
  `explicit` 为 `--to` 是否生效。
- 人类可读输出显示凭证形态与指纹，如 `Credential: api (sk-abc…wxyz)`；无法解析时
  显示 `Auth token resolved: no`。
- `--dry-run`：输出带 `(dry run) Preview only` 标注（`rotate` 本身无副作用，预览与
  实际执行一致，但 dry-run 标记会体现在输出中）。
- 没有配置账号时打印提示，退出码 `1`。

### 4.7 `check` —— 健康检查聚合输出

```
opencode-rate-limiter check [--json] [--trend]
opencode-rate-limiter check --export-events FILE [--export-format jsonl|csv]
```

- 行为：默认输出**人读摘要**（版本 / 守护进程配置与运行时 / 账号池与健康度 /
  最近探测结果 / 路径计数）；`--trend` 追加**趋势网格**（各模型近 N 轮的状态字母
  矩阵，`a`可用 `!`限流 `x`错误 `.`未探测，左→右=旧→新）与**最近事件**
  （轮换/清理/冷却/预算/重载，新→旧）；`--json` 输出完整机器可读报告。
- 输出结构：

```jsonc
{
  "timestamp": "2026-09-11T...Z",
  "config_valid": true,
  "opencode_version": "1.18.30",
  "config_paths": {
    "config_dirs":    [".../.opencode", ".../opencode", "..."],   // 全部候选目录
    "cache_dirs":     [...],   // 配置缓存目录 ∪ OpenCode 原生缓存目录，去重
    "state_files":    [...],   // 配置状态文件 ∪ OpenCode 原生状态文件，去重
    "auth_files":     [...]    // 全部候选 auth.json
  },
  "account_pool": {
    "configured_accounts": 2,
    "strategy": "health",
    "accounts": [{"name": ..., "auth_path": ..., "env_var": ...}],
    // 若守护进程状态文件含 pool_health，此处会出现：
    "health": {"primary": {"success": 12, "total": 15, "consecutive_failures": 0,
                            "avg_latency_ms": 210.5, "score": 0.83}, ...}
  },
  "daemon": {
    "interval_seconds": 30,
    "models": [...],            // 配置中的模型列表
    "auto_cleanup_on_429": true
    // 若守护进程曾写过状态文件，此处会被运行时状态合并覆盖，追加：
    // "running", "uptime_seconds", "last_probe", "next_probe", "last_cleanup",
    // "total_cycles", "total_cleanups", "models"(探测结果 dict), "pid", "updated_at"
  }
}
```

- 路径信息含义：
  - `config_dirs`：OpenCode 配置/状态候选目录（`~/.opencode`、platformdirs、平台专属）。
  - `cache_dirs` / `state_files`：CLI/文件配置的 `[cleanup]` 项经 `~`/`$VAR`/`%VAR%` 展开后
    再并上 OpenCode 原生路径，去重。
- 退出码恒为 `0`（不是健康状态码，仅表示命令成功）。

### 4.8 `diagnose` —— Zen 限额诊断

```
opencode-rate-limiter diagnose [--model MODEL] [--json]
```

一次诊断 = 环境检出 + 出口 IP 核实 + **单次探测**（仅消耗 1 次每日配额）+
429 错误层级判定 + 分项发现与建议。产物为人读报告；`--json` 输出完整结构
（`findings[]` 含 severity/title/detail/remedy）。

诊断覆盖与判定：

| 检查项 | 判定内容 |
|--------|----------|
| 出口 IP | 经 `HTTP(S)_PROXY` 实测出口（多个回显服务回退、`ipaddress` 校验），IPv6 给出 /64 前缀并提示聚合规则 |
| 代理环境 | `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`/`NO_PROXY`；提醒终端 CLI 不读系统代理、NO_PROXY 可能绕过代理 |
| 429 分层 | 解析响应体 `error.type`：`FreeUsageLimitError`（IP 日配额→附 UTC 午夜重置时刻、「换账号无效」说明）/ `RateLimitError`（key RPM）/ `server_error`（上游错误）/ `AuthError`、`RegionError` 等，各配专属解释与建议 |
| 凭证盘点 | 各 auth.json 候选的存在性/结构（single-entry / provider-keyed）/ 是否含凭证——**绝不输出凭证内容** |
| 网络 | 探测未达网关（超时/拒连）时给出代理链路排查建议 |

退出码：`0` 健康；`1` 被限流；`2` 网络/网关错误（未达限流层）。

典型输出（节选）：

```
-- 探测（1 次请求，计入每日配额）--
  模型 deepseek-v4-flash-free: rate_limited (HTTP 429, 812ms)
  error.type: FreeUsageLimitError
-- 发现 --
  [FAIL] 免费层每日配额用尽（按 IP 计数）
         网关按出口 IP 统计每日请求数（UTC 午夜重置）。……
  [WARN] 换账号对此无效
         免费配额键是出口 IP（Redis 按 IP 计数），不是账号。……
  [WARN] 确认出口 IP 与节点切换是否生效
         ……
         → 建议: 换不同地区/不同服务商的出口 IP，或等待重置。
-- 结论: 被限流 --
```

### 4.9 `daemon` —— 后台守护进程

```
opencode-rate-limiter daemon [--interval SECONDS] [--models CSV] [--json] [--dry-run]
opencode-rate-limiter daemon --once [--models CSV] [--json]   # 单轮巡检
opencode-rate-limiter daemon --stop [--json]                 # 优雅停止
```

| 选项 | 说明 |
|------|------|
| `--interval SECONDS` | 覆盖探测间隔；`< 5` 报错并退出码 `2`；省略则用配置文件值 |
| `--models CSV` | 逗号分隔模型列表，覆盖配置文件；解析后为空时报错退出码 `2` |
| `--dry-run` | 仅打印一行日志即退出（`DRY RUN: Would start daemon`），退出码 `0` |
| `--once` | 只跑一轮探测就退出（cron/任务计划友好）；有模型被限流退出码 `1`，否则 `0`；与 `--stop` 互斥 |
| `--stop` | 向锁文件记录的 pid 发 SIGTERM 并等待最多 10s；无锁/过期锁/停止成功退出码 `0`，超时 `1`；与 `--once` 互斥 |

后台运行细节见 §6。前台 Ctrl+C 优雅退出；守护进程在 Windows 下以
`add_signal_handler` 不可用时的 `signal.signal` 兜底桥接处理 SIGINT/SIGTERM。
启动时获取单实例锁（§6.4），已有存活实例时报错并以退出码 `1` 结束。

### 4.10 `generate-systemd` —— 生成 systemd 用户服务

```
opencode-rate-limiter generate-systemd
```

输出一个 systemd user unit（`ExecStart` 解析 `shutil.which("opencode-rate-limiter")`，
`Environment=OPENCODE_RATE_LIMITER_CONFIG=<平台配置目录>/config.toml`）。
该环境变量由 `Config.load()` 读取（见 §5.1）。

### 4.11 `generate-launchd` —— 生成 macOS launchd plist

```
opencode-rate-limiter generate-launchd
```

输出 plist：`Label=com.opencode.ratelimiter`，`RunAtLoad` + `KeepAlive`，
日志 `/tmp/opencode-rate-limiter.log` / `.err.log`，同样的 `OPENCODE_RATE_LIMITER_CONFIG`
环境变量（由程序读取，见 §5.1）。

### 4.12 `generate-task` —— 生成 Windows 任务计划 XML

```
opencode-rate-limiter generate-task
```

输出 UTF-8 任务 XML：登录时触发，`LeastPrivilege` 权限，网络可用才运行，
`MultipleInstancesPolicy=IgnoreNew`，命令为解析到的可执行文件 + 参数 `daemon`。

### 4.13 `generate-config` —— 生成默认配置文件

```
opencode-rate-limiter generate-config [--force] [--config PATH] [--json]
```

| 选项 | 说明 |
|------|------|
| （无 `--config`） | 写入平台配置目录下的 `config.toml`（与 `Config.load` 的默认查找位置一致） |
| `--config PATH` | 写入指定路径 |
| `--force` | 目标文件已存在时覆盖 |
| `--json` | 输出 `{"path": ..., "written": true}` |

- 行为：把内置默认值（`Config()` 默认模板）写成 TOML，作为用户编辑起点；写入后
  提示编辑 `[account_pool]` 添加账号。
- 目标已存在且未给 `--force` 时报错，退出码 `1`；不覆盖现有文件。
- 依赖可选依赖 `tomli-w`；缺失时报错退出码 `1`。

### 4.14 `completions` —— 生成 shell 补全

```
opencode-rate-limiter completions bash|zsh|fish|powershell
```

- 补全脚本由真实 CLI parser（`_completion_payload()`）派生：子命令列表、各子命令专属选项、
  `--strategy` 取值、`probe` 位置参数候选（`FREE_MODELS + all`）均为单一数据源。
- 用法见 §7。

---

## 5. 配置文件规范

### 5.1 优先级（按实际代码）

1. `--config PATH`（CLI）
2. `$OPENCODE_RATE_LIMITER_CONFIG` 环境变量（支持 `~`、`$VAR`、`%VAR%` 展开）
3. platformdirs 用户配置目录下的 `config.toml`
   - Windows：`%LOCALAPPDATA%\opencode-rate-limiter\opencode-rate-limiter\config.toml`
     （platformdirs 未指定 appauthor 时会把应用名叠加两次）
   - Linux：`~/.config/opencode-rate-limiter/config.toml`
   - macOS：`~/Library/Application Support/opencode-rate-limiter/config.toml`
4. 内置默认值

生成的服务文件（systemd / launchd / task）都会设置 `OPENCODE_RATE_LIMITER_CONFIG`
指向默认配置路径，该变量自 v0.1.0 迭代起被 `Config.load()` 实际读取。

### 5.2 完整配置示例

```toml
# 平台配置目录下的 config.toml

[daemon]
interval_seconds = 30          # 探测间隔（秒），必须 >= 5
models = [                     # 探测模型（非空）；仅用默认时整段可省略
  "deepseek-v4-flash-free",
  "nemotron-3-ultra-free",
  "big-pickle",
]
probe_timeout_seconds = 10.0   # 单次探测超时（秒），必须 > 0
auto_cleanup_on_429 = true     # 遇 429 自动清理 + 账号轮换
history_size = 20              # 探测历史环形缓冲条数

[account_pool]
strategy = "health"            # round_robin | least_used | health
health_window = 100            # 健康度滑动窗口大小（近 N 次结果）
score_weights = { success = 0.5, latency = 0.3, recency = 0.2 }  # 健康评分权重
accounts = [
  { name = "primary", auth_path = "~/.opencode/auth.json" },
  { name = "backup1", env_var = "OPENCODE_AUTH_JSON_2" },
  # { name = "inline", auth_json = "{\"access_token\":\"...\"}" },  # 每个账号必须含 name + 至少一种来源
]

[prober]
endpoint = "https://opencode.ai/zen/v1/chat/completions"  # 探测端点
ping_message = "ping"          # 探测消息内容
max_tokens = 1                 # 探测 max_tokens（>= 1）
extra_headers = {}             # 附加请求头，如 { X-Trace = "abc" }
# proxy = "http://127.0.0.1:7890"   # 可选代理（httpx >= 0.28）
http2 = false                    # HTTP/2 探测（需可选依赖 h2）
connection_pool_size = 8         # 共享连接池大小
max_retries = 0                  # 瞬时网络错即时重试次数（>= 1）；429 永不重试
# session_id = "ses_..."          # 显式 x-opencode-session；缺省每实例随机生成

[headers]
user_agent = "opencode/{version}"   # 模板仅支持 {version} 占位符
x_opencode_client = "opencode-cli"
x_opencode_version = "{version}"

[cleanup]
cache_dirs = []                # 追加的缓存目录（支持 ~、$VAR、%VAR% 展开）；默认仅用 OpenCode 原生路径
state_files = []               # 追加的状态文件；默认仅用 OpenCode 原生路径
preserve_config = true         # 必须为 true（校验强制）
```

### 5.3 字段表

#### `[daemon]`

| 字段 | 类型 | 默认 | 校验 |
|------|------|------|------|
| `interval_seconds` | int | 900 | ≥ 5（探测与真实用量共享每日 IP 配额，默认刻意保守） |
| `daily_probe_budget` | int | 200 | ≥ 0；每 UTC 日探测请求总数硬上限，0 = 不限 |
| `key_cooldown_seconds` | int | 60 | ≥ 0；账号触发 key 维度限流后的冷却秒数，0 = 不冷却 |
| `models` | list[str] | `FREE_MODELS`（8 个） | 非空 |
| `probe_timeout_seconds` | float | 10.0 | > 0 |
| `auto_cleanup_on_429` | bool | true | - |
| `history_size` | int | 20 | ≥ 1 |
| `event_history_size` | int | 50 | ≥ 1 |
| `respect_cooldown` | bool | true | 限流模型的冷却期内跳过探测（省配额） |

#### `[account_pool]`

| 字段 | 类型 | 默认 | 校验 |
|------|------|------|------|
| `strategy` | str | `health` | ∈ {round_robin, least_used, health} |
| `health_window` | int | 100 | ≥ 1 |
| `score_weights` | table | success=0.5, latency=0.3, recency=0.2 | 三键齐全、值 ∈ [0,1]、和为 1 |
| `accounts` | list[table] | `[]` | 每个元素须为 dict，含 `name`，且含 `auth_path`/`env_var`/`auth_json` 之一 |

账号读取优先级（`AccountPool.read_auth`）：`auth_json` > `env_var` > `auth_path`。

#### `[prober]`

| 字段 | 类型 | 默认 | 校验 |
|------|------|------|------|
| `endpoint` | str | Zen 端点 | 必须 http(s):// 开头 |
| `ping_message` | str | `"ping"` | - |
| `max_tokens` | int | 1 | ≥ 1 |
| `extra_headers` | table | `{}` | - |
| `proxy` | str | 无 | httpx `proxy=` 格式（缺省走环境变量代理） |
| `http2` | bool | false | 需可选依赖 `h2`，缺失时回退 HTTP/1.1 |
| `connection_pool_size` | int | 8 | ≥ 1 |
| `max_retries` | int | 0 | ≥ 0；仅连接失败/超时即时重试，HTTP 状态（含 429）永不重试 |
| `session_id` | str | 无（随机） | 非空；显式 x-opencode-session（网关强制要求） |

#### 策略算法

| 策略 | 实现 |
|------|------|
| `round_robin` | 按索引顺序取下一个（内部游标自增） |
| `least_used` | 选 `health[name].total_count` 最小的账号 |
| `health` | 选 `AccountHealth.calculate_score()` 最高的账号 |

健康度评分（`calculate_score`，0.0–1.0，越高越健康）：

```
score = success_rate * w_success
      + latency_score * w_latency
      + recency_score  * w_recency
（w_* 由 [account_pool].score_weights 配置，默认 0.5 / 0.3 / 0.2）

success_rate  = 滑动窗口内成功率（窗口为空时回退 success_count / max(total_count, 1)）
latency_score = min(1.0, max(0.0, 1.0 - (avg_latency_ms - 100) / 900))  # 钳制[0,1]
recency_score = min(1.0, hours_since_last_error / 24)          # 无错误记录按 24h 计
```

延迟为指数移动平均（新结果权重 0.2）。健康数据经 `mark_result()` 更新：
`probe` 与 daemon 的探测结果都会回写（成功 / 429 失败）；CLI `rotate` 只做选择，
不写健康数据。

#### `[headers]`

| 字段 | 类型 | 默认 |
|------|------|------|
| `user_agent` | str | `"opencode/{version}"` |
| `x_opencode_client` | str | `"opencode-cli"` |
| `x_opencode_version` | str | `"{version}"` |

> 占位符：`{version}` 与 `{model}`（R4，未指定模型时替换为空串）；早期文档提到的
> `{timestamp}` / `{random}` **未实现**。

#### `[cleanup]`

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `cache_dirs` | list[str] | `[]` | 追加清理的缓存目录；若配置了原生 `.../cache` 会去重 |
| `state_files` | list[str] | `[]` | 追加清理的状态文件；会并上原生路径去重 |
| `preserve_config` | bool | `true` | 强制为 `true`（`false` 会校验失败） |

路径展开（`Config._expand_path`）：
- `~` / `$HOME`（`Path.expanduser()`）
- 环境变量（`os.path.expandvars()`，Windows 也支持 `%VAR%`）

### 5.4 环境变量覆盖

字段级覆盖，命名规则：

```
OPENCODE_RATE_LIMITER_<SECTION>__<KEY>
```

注意 `__`（双下划线）作层分隔；单下划线属于字段名。值会按类型自动解析：
`true/false`→bool、纯数字→int、含小数→float、含逗号→list（去空格）、其余→str。

```bash
export OPENCODE_RATE_LIMITER_DAEMON__INTERVAL_SECONDS=60
export OPENCODE_RATE_LIMITER_ACCOUNT_POOL__STRATEGY=round_robin
export OPENCODE_RATE_LIMITER_DAEMON__MODELS="deepseek-v4-flash-free,nemotron-3-ultra-free"
export OPENCODE_RATE_LIMITER_HEADERS__USER_AGENT="opencode/{version}"
```

覆盖顺序：文件 → 环境变量 → 命令行（仅 `--interval` / `--models` 等显式 CLI 参数会覆盖到
守护进程；`rotate --strategy` 会覆盖策略）。若同时设置且合法，环境变量值优先生效。

### 5.5 验证规则（不满足则退出码 2）

1. `daemon.interval_seconds >= 5`
2. `daemon.probe_timeout_seconds > 0`
3. `daemon.models` 非空
4. `account_pool.strategy` ∈ {round_robin, least_used, health}
5. `accounts[*]` 为 dict 且含 `name`，且含至少一种认证来源字段
6. `cleanup.preserve_config == true`

---

## 6. 守护进程模式

### 6.1 运行状态机

```
START
  → _install_signal_handlers()
  → 循环:
       _probe_cycle():
         1. total_cycles += 1；last_probe = now(UTC)
         2. 并发探测全部模型（probe_all）
         3. 逐条记录 model_results、打日志（含 retry_after 警告）
         4. 对每个 rate_limited 结果:
              a. 若有账号池且账号数 > 1：
                 mark_result(当前账号, 失败) → get_next() → 日志记录轮换方向
              b. 若 auto_cleanup_on_429：异步执行 full_cleanup()，
                 total_cleanups += 1、last_cleanup = now
         5. 限流冷却：对 rate_limited 模型按 retry_after（缺失用估算值）设置冷却期，
            冷却期内的模型在后续周期跳过探测（respect_cooldown 可关）；恢复可用即解除
         6. 自动清理每轮至多一次（即使同轮多个模型限流）
         7. 连续全错周期计数（error_streak）：全 error → +1，否则清零
         8. 账号健康快照写入状态（pool_health）
         9. next_probe = now + interval × 退避倍数（1×/2×/4×/8×，见 §6.1.1）；
            _persist_state()
       _wait(interval × 退避倍数)   # 监听 stop/probe 事件，超时继续

  6.1.1 退避：整轮探测全部 error（网络/端点故障）时等待间隔指数放大
         1×→2×→4×→8×（上限 8×），任一探测恢复正常即复位；放大时打 warning。
```

```
  → 收到 SIGTERM/SIGINT → 停止循环
  → finally: 还原信号处理器 → 写最后一次状态 → 释放单实例锁
```

### 6.2 信号处理

| 信号 | 行为 |
|------|------|
| `SIGTERM` (15) | 优雅停止：结束当前周期，写入状态后退出 |
| `SIGINT` (2) | 同上（Ctrl+C） |
| `SIGHUP` (1) | 重新加载配置（`Config.load` 重读，保留 CLI 覆盖的 interval/models），重建运行时组件 |
| `SIGUSR1` (10) | 立即触发一次探测（扰动 `_wait`） |
| `SIGUSR2` (12) | 把当前状态 `DaemonStatus.to_dict()` 打到日志 |

- Unix 优先用 `loop.add_signal_handler`；`NotImplementedError`（如 Windows）时回退到
  `signal.signal` + `loop.call_soon_threadsafe` 桥接。上述 5 个信号里，Windows 只有 SIGINT/
  SIGTERM 可用，其余会被跳过。
- 所有信号处理器在退出时自动还原（`_restore_signal_handlers`）。

### 6.3 状态文件

位置（platformdirs `user_state_dir`）：

| 平台 | 路径 |
|------|------|
| Windows | `%LOCALAPPDATA%\opencode-rate-limiter\opencode-rate-limiter\daemon.json` |
| Linux | `~/.local/state/opencode-rate-limiter/daemon.json` |
| macOS | `~/Library/Application Support/opencode-rate-limiter/daemon.json` |

内容（`DaemonStatus.to_dict()` + `pid` + `updated_at`）：

```
running / uptime_seconds / last_probe / next_probe / last_cleanup
total_cycles / total_cleanups / models(按名排序的探测结果)
pool_health(账号健康快照: success / total / consecutive_failures / avg_latency_ms / score)
history(探测历史环形缓冲: [{ts, models: {模型: 状态}}]，最多 history_size 条)
events(决策事件审计环: [{ts, kind, ...}——cooldown_armed/key_cooldown/rotation/
cleanup/budget_exhausted/reload]，最多 event_history_size 条)
cooldowns(模型 → 冷却截止的绝对 UTC 时刻，重启后恢复剩余时间)
probe_usage({day, count}——每日探测预算计数，跨重启恢复)
pid / updated_at
```

写入方式：`<path>.tmp` 临时文件 + `os.replace` 原子替换；写失败不报错（仅 debug 日志）。
`opencode-rate-limiter check --json` 读取该文件并把 `pool_health` 归位到输出的
`account_pool.health`（不再出现在 `daemon` 段），其余运行时字段合并覆盖 `daemon` 配置。

### 6.4 单实例锁

`daemon` 启动时在状态目录创建 `daemon.lock`（内容为 pid）并在同目录哨兵文件
`daemon.flock` 上持有 OS 文件锁（POSIX `flock` / Windows `msvcrt.locking`，
进程持有至退出，OS 在进程死亡时自动释放）：

| 情形 | 行为 |
|------|------|
| 哨兵锁可获取 | 写入 pid，正常启动（过期 pid 直接覆盖并打 warning） |
| 哨兵锁被占且 pid 存活 | 报错 `another daemon instance appears to be running (pid ...)`，退出码 `1` |
| 哨兵锁被占但 pid 已死 / 内容损坏 | 拒绝启动并报错（活锁持有者存在，不可损坏其状态） |

- 进程退出（含信号优雅停止）时解锁、关闭并删除锁文件（仅当锁内 pid 仍是自己时删）。
- 哨兵与 pid 分离的原因：Windows 字节锁会拒绝同文件的其他打开，pid 文件须保持可读。
- 停止运行中的实例用 `daemon --stop`（SIGTERM + 最多 10s 等待），不要直接删锁文件。
- Windows 下存活检测用 `OpenProcess`/`GetExitCodeProcess`（`os.kill(pid, 0)` 在
  Windows 会**终止**进程，绝不使用）；POSIX 用 `os.kill(pid, 0)`。
- 锁路径与状态文件同目录：`user_state_dir("opencode-rate-limiter")/daemon.lock`。

### 6.5 部署

#### systemd（Linux 用户级）

```bash
opencode-rate-limiter generate-systemd > ~/.config/systemd/user/opencode-rate-limiter.service
systemctl --user daemon-reload
systemctl --user enable --now opencode-rate-limiter
systemctl --user status opencode-rate-limiter
```

生成的 unit 含 `Type=exec`、`Restart=on-failure`、`RestartSec=10`、内置资源上限
`MemoryMax=100M` 与 `CPUQuota=10%`（模板中标注为「可选: 限制资源」），并设置
`Environment=OPENCODE_RATE_LIMITER_CONFIG=...`（被 `Config.load()` 读取）。

#### launchd（macOS）

```bash
opencode-rate-limiter generate-launchd > ~/Library/LaunchAgents/com.opencode.ratelimiter.plist
launchctl load ~/Library/LaunchAgents/com.opencode.ratelimiter.plist
launchctl start com.opencode.ratelimiter
```

#### Windows 任务计划

```powershell
opencode-rate-limiter generate-task > "$env:TEMP\opencode-rate-limiter.xml"
schtasks /create /xml "$env:TEMP\opencode-rate-limiter.xml" /tn "OpenCode Rate Limiter"
schtasks /run /tn "OpenCode Rate Limiter"
```

### 6.6 监控

- **状态查询**：`opencode-rate-limiter check --json`（合并运行时状态）。
- **事件导出**：`check --export-events FILE [--export-format jsonl|csv]` 把决策
  事件环落盘（提 issue/复盘材料）；无 daemon 状态时退出码 `1`，格式非法 `2`。
- **日志**：默认 INFO 打 stderr；`--json` 时每行为一条精简 JSON：
  `{timestamp, level, logger, message, ...extra}`（异常自动带 `exc` 堆栈）；
  `--json-verbose` 追加 `module`/`function`/`line`；`-vv`（DEBUG）最详细。
  `--log-file PATH` 同时落盘（1MB×4 轮转），daemon 无需外部收集器。
- **日志器名**：`main`、`cmd.*`、`daemon`、`prober`、`cleanup`、`pool`。
  `httpx` / `httpcore` 日志被抑制到 WARNING。

---

## 7. Shell 补全

### 7.1 生成与安装

```bash
# bash
opencode-rate-limiter completions bash > ~/.local/share/bash-completion/completions/opencode-rate-limiter.bash

# zsh
opencode-rate-limiter completions zsh > ~/.zsh/functions/_opencode-rate-limiter

# fish
opencode-rate-limiter completions fish > ~/.config/fish/completions/opencode-rate-limiter.fish
```

```powershell
# PowerShell（追加到 $PROFILE）
opencode-rate-limiter completions powershell >> $PROFILE
```

也可以一次性重写仓库内的 `completion/` 脚本：

```bash
python scripts/generate_completions.py
```

### 7.2 覆盖范围

- 子命令（基于真实 parser 的 `help`/`description`，与 `-h` 输出同源）
- 全局选项：`--config`、`--json`、`--json-verbose`、`--verbose`、`--quiet`、
  `--dry-run`、`--log-file`、`--version`、`--help`、`-h`
- 各子命令专属选项（如 `probe --model`、`rotate --strategy`、`daemon --interval/--models`）
- `--strategy` 的三值、`--config` 的文件路径补全（bash）、`probe` 位置参数的模型列表
  （`FREE_MODELS + all`）
- bash 服务端式 `case` 按 `prev` 上下文分支；zsh 用 `_arguments -C`；fish 用
  `__fish_seen_subcommand_from` 约束子命令上下文。

---

## 8. 二进制打包

```bash
pip install ".[build]"        # 安装 pyinstaller
python scripts/build_binary.py
```

行为（`scripts/build_binary.py`）：
- 产物输出 `dist/opencode-rate-limiter-<platform>-<arch>[.exe]`，先清空 `dist/`。
- 单文件（`--onefile`），自动带上 `httpx`、`platformdirs`、`tomli_w`（实测 13.0 MB）。
- 入口经由自动生成的 shim（`build/_pyinstaller_entry.py`）：包内相对导入无法直接作为
  PyInstaller 入口，脚本以 `--paths <项目根>` 保证包可被解析。
- `--strip` 仅非 Windows 启用；`--uac-admin` / `--icon` 不启用。
- macOS 附带 `com.opencode.ratelimiter` bundle identifier。
- 输出不使用非 ASCII 符号（控制台为 GBK 等编码时避免 UnicodeEncodeError）。
- 构建完成自动以 `--help` 冒烟验证；退出码非 0 视为构建失败。
- `.spec` 文件落在项目根（PyInstaller 默认），每次构建前按 `*.spec` 通配清理。

---

## 9. 退出码

| 退出码 | 场景 |
|--------|------|
| 0 | 成功；`--help` / `--version` |
| 1 | `quick`/`deep` 清理存在错误；`probe` 有任意模型被限流；`rotate` 无账号；daemon 已有存活实例（单实例锁）；`generate-config` 目标已存在（无 `--force`）或写入失败；命令处理器抛未捕获异常 |
| 2 | 配置加载失败（文件缺失/TOML 语法错误等）；无效子命令或缺失必需参数（argparse）；`daemon --interval < 5`；`daemon --models` 解析为空 |
| 130 | `asyncio.run` 阶段捕获 `KeyboardInterrupt`（含 `daemon` 在信号兜底不可用时的 Ctrl+C） |

> 说明：子命令为 `argparse` 必选项，未传命令或传了未知命令都会由 argparse 以退出码 2
> 报错终止；`main()` 中 `COMMAND_HANDLERS.get(...)` 的 `print_help(); return 1` 分支实际为
> 防御性死代码。

---

## 10. 故障排查

### 10.1 快速诊断

```
opencode-rate-limiter check --json | jq .
```

- `daemon.running == true` 且各模型 `models.*.status` → 查看探测结果
  （存储于 check 输出 `daemon.models` 字典，按模型名）。
- `account_pool.configured_accounts` → 账号是否配置。
- `config_paths.*` → 实际解析出的目录/文件清单，用于核对清理目标。

### 10.2 常见问题

| 问题 | 排查/解决 |
|------|-----------|
| 配置不生效 | 确认文件在 `--config` 指定路径或平台配置目录；`check --json` 看 `daemon.interval_seconds` 等实际值 |
| 版本号不符 | `opencode` 需在 PATH；版本检测失败会回退 `unknown`（头部模板插入 `unknown`）；可用 `OPENCODE_VERSION=xxx` 覆盖 |
| 探测全 error(timeout) | 网络问题；配置 `[prober].proxy` 或标准 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量，或调大 `probe_timeout_seconds` |
| 探测出现 429 | 服务端按 IP 计数的每日配额用尽（UTC 午夜重置）；`estimated_reset` 即冷却秒数；本地操作无法解除 |
| 本地总提示限流 | 服务端冷却未结束，本地清理只解除本地挂起；等待 1–5 分钟 |
| `rotate` 无账号 | `[account_pool]` 未配置或账号缺 `name`/认证来源；`check --json` 看 `configured_accounts` |
| `check --json` 没有文件输出 | 结构化命令不打印横幅是正常行为，输出即为 JSON |
| 401 | token 失效：重新 `opencode login` 或更换 `auth_path` 指向的 auth.json |
| 守护进程不驻留 | systemd 场景先 `journalctl --user -u opencode-rate-limiter -n 50`；确认 `ExecStart` 解析到的路径真实存在 |

### 10.3 日志查看

```bash
# DEBUG 级（包含探测延迟细项）
opencode-rate-limiter -vv check
# 轮换与清理事件（daemon 运行中）
opencode-rate-limiter -vv daemon
```

---

## 11. 开发与质量保障

### 11.1 测试

```bash
uv run pytest -q        # 294 passed
```

覆盖：核心清理（dry-run/备份/缓存）、探测（httpx mock 200/429/超时）、账号池读取
（auth_path/env_var/auth_json）、守护进程信号与状态持久化、补全生成、打包脚本（importlib
动态加载）。测试对单一入口模块采用 importlib 动态加载以避免路径假设。

### 11.2 静态检查

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
```

- ruff：`line-length=100`；规则集 E/F/I/UP/B/C4/PTH/T20/ARG/SIM/N/Q/RUF；
  忽略 S101、PTH123、ARG001/2、T201、RUF001/2/3、`UP031`（`%`-format 保留给含
  `{cur}` / `{${commands}}` 的大括号密集模板，pyproject 内有理由注释）。
- mypy：主源码严格（`strict`），测试经 pyproject overrides 放宽但仍参与类型检查。
- pre-commit：`.pre-commit-config.yaml` 校验通过（在无 `--dev` 环境下可用 PyYAML 做
  CI 侧校验）。

### 11.3 版本一致性

发布前以 `uv run python scripts/check_version.py <版本>` 核对全部版本标记一致：
`opencode_rate_limiter.__version__`、`pyproject.toml`、`man/opencode-rate-limiter.1`、
`MANUAL.md` 头、四份 `docs/*.md` 头、`README.md` 状态行，且 `CHANGELOG.md` 含对应
`## [<版本>]` 条目。发布流程（tag → CI 产物）见 `docs/release.md`；
变更记录见 `CHANGELOG.md`。

---

## 12. 实现事实与文档差异

早期 `docs/` 拆分文档（已移除）中**大量内容属于规划/未尽实现**。以下条目为历史证伪记录：

| 文档声称 | 实际代码 | 影响 |
|----------|----------|------|
| ~~`$OPENCODE_RATE_LIMITER_CONFIG` 不被读取~~（已修复） | `Config.load()` 现按 `--config` > 该环境变量 > 默认目录解析（见 §5.1） | 已解决 |
| ~~`quick`/`deep` 行为相同~~（已拆分） | `quick` = 清锁+重置 token（不清缓存）；`deep` = +清缓存+重新登录提示（见 §4.2/§4.3） | 已解决 |
| 自定义策略 `custom_strategy = "..."`（advanced.md） | `strategy` 仅支持三个固定值；校验对未知值直接拒绝 | 不可用 |
| ~~`[prober]` endpoints / custom_headers / probe_payloads / http2 / connection_pool_size~~ | 已实现 `[prober]` 配置段：`endpoint`/`extra_headers`/`ping_message`/`max_tokens`/`proxy`（http2 与连接池大小仍硬编码） | 部分可用（http2/连接池不可配） |
| 清理分级 `level1/2/3_files`、`[cleanup.scheduler]` 定时清理（advanced.md） | 无此字段；无定时器 | 不可用 |
| `--concurrent` / `--parallel`（advanced.md） | 无此类参数 | 探测固定并发（gather），清理固定串行 |
| Token 加密 `[account_pool].encryption`、`[audit]`、`[plugins]`（advanced.md） | 无 | 不可用 |
| 头模板变量 `{timestamp}` / `{random}`（configuration.md） | 仅 `{version}` 被 `.format()` 替换 | 写入即字面量 |
| ~~文件锁防并发守护进程~~（已实现） | daemon 启动时获取单实例锁，存活实例报错退出，过期锁自动接管（见 §6.4） | 已解决 |
| ~~`OPENCODE_VERSION=xxx` 覆盖版本~~（已实现） | `OPENCODE_VERSION` 非空时优先于 `opencode --version` | 已解决 |
| ~~`rotate` 是「名义轮换」~~（已解决） | `probe`/daemon 按账号轮换为探测注入 `Authorization`，结果回写健康度（见 §2.3）；`rotate --apply` 可把账号 auth 真实写入 `auth.json`（带备份） | 已解决 |
| ~~`rotate --dry-run` 被静默忽略~~（已修复） | `--dry-run` 体现在输出（`dry_run` 字段 / `(dry run)` 标注） | 已解决 |
| 配置校验「accounts 非空才可 rotate/daemon」 | 校验只要求字段合法；空账号池被允许（daemon 跳过池、rotate 报退出码 1） | 行为比文档宽松 |
| 代理经配置段设置（troubleshooting.md） | 无配置段；httpx 默认遵循标准 `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` 环境变量 | 代理可用但无法精细配置 |
| check 输出含顶层 `models.*`、`account_pool.current` 等（troubleshooting.md 决策树） | 顶层无 models；探测结果在 `daemon.models`；账号池无 `current`/`healthy` 明细 | 决策树需按 §4.7 的结构调整 |
| `docs/architecture.md` 决策记录「Python 3.11+/asyncio/并发」 | 与实现一致 | 无需更正 |
| 健康度「近 100 次请求」滑动窗口（advanced.md 注释） | 已实现滑动窗口（`health_window`，默认 100）+ EMA 延迟（权重 0.2） | 已解决 |

> 处理建议：旧文档所列「进阶用法」「监控告警」「安全加固」「扩展开发」等均为未实现
> 的规划，使用时须以本手册与 `docs/` 四份现行文档为准。

## 13. 已知限制

1. **账号轮换默认不写盘**：`probe`/daemon 的探测按账号注入 `Authorization` 并回写健康度；
   `rotate` 默认只做选择与 token 解析校验，加 `--apply` 才会把账号 auth 写入
   OpenCode 的 `auth.json`（带备份），且不影响 OpenCode CLI 正在运行中的会话。
2. **凭证解析覆盖 oauth/api 双形态**（`access`/`access_token`/`key`，顶层或一层
   嵌套）；OpenCode auth 结构再变化时需扩展 `extract_credential`。
3. **头模板仅支持 `{version}` / `{model}`** 两个占位符。
4. **探测与真实用量共享每日 IP 配额**：网关按请求数（而非 token 数）对免费层计费，
   每次探测都计入同一 IP 的每日额度。默认 `daily_probe_budget=200` 硬上限 +
   900s 间隔即是为此；调高预算等于挤占真实使用额度。

---

## 附录 A：OpenCode Zen 限额机制（源码核实，2026-09）

以下结论来自 opencode 仓库 `packages/console/app/src/routes/zen/` 网关源码（dev 分支），
非猜测。限流状态全部存服务端（Redis / MySQL），**本地不存在任何可清除的限流状态**。

| 层 | 限流器 | 键 | 窗口 | 说明 |
|----|--------|----|------|------|
| 1 | `ipRateLimiter`（免费层核心） | IP（`x-real-ip`；IPv6 取前 4 段） | UTC 自然日 | 每日请求数（数值在部署配置 `ZEN_LIMITS`，社区实测约 200–600/天）；新 IP 前 7 天额度 ×2；特定模型可配独立日桶 |
| 2 | `keyRateLimiter` | Zen API key + 模型 | 每分钟 | 默认 1000 RPM/key（付费路径） |
| 3 | `trialLimiter` | IP（数据库累计） | 长期 | 试用 provider 的 token 预算 |
| 4 | `modelTpmLimiter` | 模型（全体用户共享） | 每分钟 | 输入 token 总量；撞上属全局拥堵，等即可 |
| 5 | 订阅/余额 | 账号 | 5h/周/月 | Go/订阅计费路径 |

关键事实：

1. **官方客户端头检查当前被网关注释禁用**（`headersExist = true` 硬编码），头信息仅用于
   打点。`HeaderInjector` 保留为机制回归时的低成本保险。
2. **429 响应携带 `retry-after` 头**，值 = 距 UTC 午夜的秒数（免费层）；本工具的冷却期
   直接采用该值。
3. **opencode CLI 无本地限流状态**：不存在 `state.json`、`rate_limited_until`、
   `*rate_limit*.json`；auth.json 结构为 `{provider: {type: "oauth", access, refresh,
   expires}}` 或 `{type: "api", key}`。
4. **探测请求计入同一 IP 的每日配额**——守护进程默认 900s 间隔 + 每日 200 次预算，
   即为此设计的止血措施。
5. 真正能提高可用量的只有：等待 UTC 午夜重置、更换出口 IP（注意 IPv6 /64 粒度）、
   付费 Zen key / BYOK。多账号在同 IP 下对免费层**无效**；账号池仅对付费 key 维度有意义。
