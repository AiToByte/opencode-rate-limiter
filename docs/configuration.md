# 配置文件完整规范

## 配置文件位置

优先级从高到低：
1. `--config /path/to/config.toml` (CLI 参数)
2. `$OPENCODE_RATE_LIMITER_CONFIG` (环境变量)
3. `~/.config/opencode-rate-limiter/config.toml` (platformdirs 用户配置目录)
4. 内置默认值

## 完整配置示例

```toml
# ~/.config/opencode-rate-limiter/config.toml

[daemon]
# 守护进程探测间隔（秒）
interval_seconds = 30
# 探测的模型列表，逗号分隔或数组
models = ["deepseek-v4-flash-free", "nemotron-3-ultra-free", "big-pickle", "mimo-v2.5-free"]
# 单次探测超时（秒）
probe_timeout_seconds = 10.0
# 遇到 429 时自动触发清理和账号轮换
auto_cleanup_on_429 = true

[account_pool]
# 账号池配置，支持多种来源
accounts = [
  # 本地文件路径
  { name = "primary", auth_path = "~/.opencode/auth.json" },
  { name = "backup1", auth_path = "~/.config/opencode/auth-backup1.json" },
  # 环境变量 (JSON 字符串或文件路径)
  { name = "env_account", env_var = "OPENCODE_AUTH_JSON_2" },
  # 直接内嵌 (不推荐，敏感信息)
  # { name = "inline", auth_json = "{...}" }
]
# 轮换策略: round_robin | least_used | health
strategy = "health"

[headers]
# 官方 CLI 兼容头部模板
# {version} 会自动替换为检测到的 opencode 版本
user_agent = "opencode/{version}"
x_opencode_client = "opencode-cli"
x_opencode_version = "{version}"

[cleanup]
# 需要清理的缓存目录 (支持 ~ 展开)
cache_dirs = [
  "~/.opencode/cache",
  "~/Library/Caches/opencode",           # macOS
  "%APPDATA%/opencode/cache",            # Windows
  "$XDG_CACHE_HOME/opencode"             # Linux XDG
]
# 需要重置的状态文件
state_files = [
  "~/.opencode/state.json",
  "~/Library/Application Support/opencode/state.json",
  "%APPDATA%/opencode/state.json"
]
# 保护用户主配置，绝不删除 config.json
preserve_config = true
```

## 字段详细说明

### `[daemon]` - 守护进程配置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `interval_seconds` | integer | 30 | 探测间隔，建议 30-300 秒 |
| `models` | array of strings | 8 个免费模型 | 探测目标，可自定义子集 |
| `probe_timeout_seconds` | float | 10.0 | 单次 HTTP 探测超时 |
| `auto_cleanup_on_429` | boolean | true | 429 时自动清理+轮换 |

### `[account_pool]` - 账号池配置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `accounts` | array of tables | `[]` | 账号列表，至少 1 个 |
| `strategy` | string | `"health"` | 轮换策略 |

#### 账号表字段

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 唯一标识符 |
| `auth_path` | string | 否* | auth.json 文件路径 |
| `env_var` | string | 否* | 环境变量名 |
| `auth_json` | string | 否* | 直接内嵌 JSON (不推荐) |

*三选一，优先级：`auth_json` > `env_var` > `auth_path`

#### 轮换策略详解

| 策略 | 行为 | 适用场景 |
|------|------|----------|
| `round_robin` | 顺序轮询 | 账号权重均等 |
| `least_used` | 使用次数最少优先 | 负载均衡 |
| `health` | 健康度评分最高优先 | 生产环境推荐 |

健康度评分因子：
- 成功率 (权重 50%)
- 平均延迟 (权重 30%)
- 最近错误时间 (权重 20%)

### `[headers]` - 请求头模板

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `user_agent` | string | `"opencode/{version}"` | User-Agent 模板 |
| `x_opencode_client` | string | `"opencode-cli"` | 固定值 |
| `x_opencode_version` | string | `"{version}"` | 版本头模板 |

支持变量：
- `{version}` - 检测到的 opencode 版本 (如 `1.18.16`)
- `{timestamp}` - 当前 Unix 时间戳
- `{random}` - 随机字符串 (防缓存)

### `[cleanup]` - 清理配置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `cache_dirs` | array of strings | 平台默认 | 缓存目录列表 |
| `state_files` | array of strings | 平台默认 | 状态文件列表 |
| `preserve_config` | boolean | `true` | 保护 config.json |

#### 路径变量展开

支持以下变量自动展开：
- `~` / `$HOME` / `%USERPROFILE%` - 用户主目录
- `$XDG_CONFIG_HOME` / `$XDG_CACHE_HOME` / `$XDG_STATE_HOME` - XDG 目录
- `%APPDATA%` / `%LOCALAPPDATA%` - Windows 应用数据
- `~/Library/Application Support` - macOS 应用支持

---

## 环境变量覆盖

所有配置项均可通过环境变量覆盖，命名规则：
```
OPENCODE_RATE_LIMITER_<SECTION>__<KEY>
```
注意：双下划线 `__` 分隔 section 和 key（单下划线用于字段名本身）。

示例：
```bash
export OPENCODE_RATE_LIMITER_DAEMON__INTERVAL_SECONDS=60
export OPENCODE_RATE_LIMITER_ACCOUNT_POOL__STRATEGY=round_robin
export OPENCODE_RATE_LIMITER_CLEANUP__PRESERVE_CONFIG=false
```

数组类型用逗号分隔：
```bash
export OPENCODE_RATE_LIMITER_DAEMON__MODELS="deepseek-v4-flash-free,nemotron-3-ultra-free"
```

---

## 配置验证规则

启动时自动验证：
1. `daemon.interval_seconds` ≥ 5
2. `daemon.probe_timeout_seconds` > 0
3. `account_pool.accounts` 非空 (若使用 rotate/daemon)
4. `account_pool.strategy` ∈ {round_robin, least_used, health}
5. `cleanup.preserve_config` = true (强制，不可改)

验证失败将阻止启动并提示错误。

---

## 最小配置示例

```toml
# 仅使用默认值，无账号池
[daemon]
models = ["deepseek-v4-flash-free"]
```

---

## 生产环境推荐配置

```toml
[daemon]
interval_seconds = 60
models = ["deepseek-v4-flash-free", "nemotron-3-ultra-free", "big-pickle"]
probe_timeout_seconds = 15.0
auto_cleanup_on_429 = true

[account_pool]
accounts = [
  { name = "primary", auth_path = "~/.opencode/auth.json" },
  { name = "backup1", auth_path = "~/.config/opencode/auth-backup1.json" },
  { name = "backup2", auth_path = "~/.config/opencode/auth-backup2.json" }
]
strategy = "health"

[headers]
user_agent = "opencode/{version}"
x_opencode_client = "opencode-cli"
x_opencode_version = "{version}"

[cleanup]
cache_dirs = ["~/.opencode/cache"]
state_files = ["~/.opencode/state.json"]
preserve_config = true
```