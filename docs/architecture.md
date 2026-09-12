# 架构设计文档

## 核心问题：OpenCode 免费模型限流机制深度解析

### 1. 限流识别机制

OpenCode Zen 免费模型通过 **HTTP 请求头** 识别官方客户端：

| 头部 | 官方值 | 说明 |
|------|--------|------|
| `User-Agent` | `opencode/x.y.z` | 必需，版本号需匹配 |
| `x-opencode-client` | `opencode-cli` | 必需，标识客户端类型 |
| `x-opencode-version` | `x.y.z` | 必需，版本号 |

**无头部 = 匿名客户端 = 极严限流 (约 1-5 RPM)**

### 2. 免费模型池 (2026-08 時点)

| 模型 ID | 提供商 | 端点 | 限额 |
|---------|--------|------|------|
| `deepseek-v4-flash-free` | DeepSeek | `/zen/v1/chat/completions` | ~15-20 RPM |
| `big-pickle` | Stealth | `/zen/v1/chat/completions` | ~15-20 RPM |
| `mimo-v2.5-free` | Xiaomi | `/zen/v1/chat/completions` | ~15-20 RPM |
| `nemotron-3-ultra-free` | NVIDIA | `/zen/v1/chat/completions` | ~15-20 RPM |
| `hy3-free` |  | `/zen/v1/chat/completions` | ~15-20 RPM |
| `laguna-s-2.1-free` |  | `/zen/v1/chat/completions` | ~15-20 RPM |
| `ling-3.0-flash-fin-free` |  | `/zen/v1/chat/completions` | ~15-20 RPM |
| `nemotron-3.5-lightning-free` | NVIDIA | `/zen/v1/chat/completions` | ~15-20 RPM |

**统一端点**: `https://opencode.ai/zen/v1/chat/completions` (OpenAI 兼容)

### 3. 限额规格

| 维度 | 免费版 | 说明 |
|------|--------|------|
| 日请求数 | 100 req/day | 账号级 |
| 分钟请求数 | ~15-20 RPM | 单模型，滑动窗口 |
| Token/分钟 | 未公开 | 估算受 TPM 影响 |
| 并发连接 | 未公开 | 估算较低 |

### 4. 错误信号

```json
// HTTP 429 FreeUsageLimitError
{
  "type": "error",
  "error": {
    "type": "FreeUsageLimitError",
    "message": "Error from provider (Console): Rate limit exceeded. Please try again later."
  }
}
```

**关键特征**：
- 无标准 `Retry-After` 头部
- 无 `X-RateLimit-*` 头部
- **Silent Limit** - 客户端需自行估算冷却时间

### 5. 本地状态持久化

OpenCode CLI 在本地记录限流状态：

| 文件 | 关键字段 | 作用 |
|------|----------|------|
| `~/.opencode/state.json` | `backoff`, `rate_limited_until` | 本地退避等待 |
| `~/.opencode/auth.json` | `access_token`, `rate_limited_until` | 凭证层限流标记 |
| `~/.opencode/cache/*rate_limit*.json` | 缓存的限流锁 | 缓存层限流 |

清理这些文件可**强制解除本地挂起**，但需配合服务端冷却。

### 6. 缓存膨胀与 TPM 压迫

长对话上下文缓存导致请求体积增大，触发上游 TPM 限制：
- Context Cache 积累 → 请求 Token 数激增 → 触发 TPM 阈值 → 429
- 定期清理 `~/.opencode/cache/` 可缓解

---

## 工具架构设计

### 核心模块

```
┌─────────────────────────────────────────────────────────┐
│                    opencode-rate-limiter                   │
├─────────────────────────────────────────────────────────┤
│  CLI Layer (argparse + asyncio)                           │
├─────────────────────────────────────────────────────────┤
│  Config Manager (TOML + platformdirs + 默认值)             │
├─────────────────────────────────────────────────────────┤
│  Core Services:                                           │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐     │
│  │ HeaderInject │ │ ModelProber  │ │ AccountPool  │     │
│  └──────────────┘ └──────────────┘ └──────────────┘     │
│  ┌──────────────┐ ┌──────────────┐                       │
│  │ CleanupMgr   │ │ Daemon       │                       │
│  └──────────────┘ └──────────────┘                       │
├─────────────────────────────────────────────────────────┤
│  Utils: Logging (JSON/Structured), Path Resolution       │
└─────────────────────────────────────────────────────────┘
```

### 数据流

```
用户调用 CLI
    ↓
加载配置 (CLI > ENV > 文件 > 默认)
    ↓
实例化核心服务
    ↓
分发到对应命令处理器
    ↓
  ├─ quick/deep → CleanupManager → 文件系统操作
  ├─ probe → ModelProber → HTTP 探测 → ProbeResult
  ├─ headers → HeaderInjector → 头部模板 → JSON/ENV
  ├─ rotate → AccountPool → 轮换策略 → 更新 auth.json
  ├─ check → 聚合健康检查 → JSON 输出
  └─ daemon → Daemon.run() → 信号处理 + 定时任务
```

### 守护进程状态机

```
START
  ↓
INIT (加载配置、初始化服务)
  ↓
PROBE_LOOP (每 interval 秒)
  ├─ 并发探测所有配置模型
  ├─ 收集 ProbeResult
  ├─ 处理结果:
  │   ├─ available → 记录健康
  │   ├─ rate_limited → 触发清理 + 账号轮换 (若启用)
  │   ├─ error → 记录错误、指数退避
  │   └─ unknown → 记录
  └─ 睡眠 interval 秒
  ↓
收到信号 (SIGTERM/SIGINT)
  ↓
SHUTDOWN (取消任务、清理资源)
  ↓
EXIT
```

---

## 设计决策记录

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 配置格式 | TOML/JSON/YAML | **TOML** | Python 3.11+ 标准库 `tomllib`，零依赖 |
| 守护进程 | asyncio/systemd 生成 | **asyncio** | 跨平台统一，无需用户配置 systemd |
| 头部注入 | 硬编码/模板 | **模板** | 支持版本变量、用户自定义 |
| 探测并发 | 串行/并发 | **并发** | 减少总探测时间 |
| 单文件分发 | 是/否 | **是** | uvx 友好，PyInstaller 打包 |
| Python 版本 | 3.10/3.11+ | **3.11+** | `tomllib`、`ExceptionGroup`、`TaskGroup` |

---

## 依赖关系图

```
opencode-rate-limiter
├── 标准库: asyncio, json, logging, pathlib, argparse, tomllib (3.11+)
├── httpx (HTTP 客户端 + HTTP/2 支持)
├── tomli-w (TOML 写入)
├── platformdirs (跨平台目录)
└── 开发依赖: pytest, pytest-httpx, pytest-asyncio, ruff, mypy, pyinstaller
```