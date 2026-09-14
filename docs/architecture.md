# opencode-rate-limiter 技术架构文档

版本：0.6.2（2026-09） · 适用代码：`opencode_rate_limiter/` 包（当前 main）

> 本文回答"系统为什么长这样"。实现层面的"怎么做的"见 [implementation.md](implementation.md)，
> 功能层面的"能做什么"见 [features.md](features.md)，操作层面的"怎么用"见
> [user-guide.md](user-guide.md) 与 [MANUAL.md](../MANUAL.md)（命令/配置权威参考）。

---

## 1. 背景与架构约束

本工具服务于一个已由源码核实的外部事实（详见 [features.md §1](features.md)）：
**OpenCode Zen 免费模型的限额完全在服务端**——按出口 IP 的 UTC 日请求计数（Redis）、
按 key 的 RPM、全局共享的模型 TPM，全部状态存于服务端 Redis/数据库；opencode CLI
本地不存在任何限流状态；429 响应携带 `retry-after`（= 距 UTC 午夜的秒数）。

这决定了四条不可违背的架构约束：

| # | 约束 | 架构回应 |
|---|------|----------|
| C1 | 本地操作无法解除服务端限额 | 系统定位为**可观测 + 配额保护**，不做任何"解除"承诺；清理功能只做真实存在的事（auth 备份、缓存维护） |
| C2 | 探测请求与真实用量共享同一 IP 每日配额 | 探测必须有**预算硬上限**（`daily_probe_budget`）和保守默认间隔（900s） |
| C3 | 429 的重置点是 UTC 午夜 | 冷却期/估算重置统一走 `seconds_to_utc_midnight()`，绝不猜测 60s |
| C4 | 免费层限额键是 IP，账号不参与 | 账号池明确标注仅对付费 key / BYOK 维度有效；诊断功能主动输出"换账号无效"结论 |

## 2. 总体结构

单包、单依赖方向、无循环。入口点 `opencode_rate_limiter:main` 与公共 API 由
`__init__.py` 统一再导出。

```
                        ┌────────────────────────────┐
                        │  cli.py                    │
                        │  cmd_* / COMMAND_HANDLERS  │
                        │  main()                    │
                        └─────┬──────────────────────┘
              ┌───────────────┼───────────────────────┐
              ▼               ▼                       ▼
      ┌──────────────┐ ┌─────────────┐      ┌──────────────────┐
      │ parser.py    │ │ completions │      │ daemon/ 包       │
      │ build_parser │ │ (生成补全)  │      │ _runner/_lock/   │
      └──────┬───────┘ └──────┬──────┘      │ _state           │
              │                │             └───────┬──────────┘
              │                │                     │
              ▼                ▼                     ▼
      ┌──────────────────────────────────────────────────────┐
      │ 服务层                                                │
      │  prober.py    ModelProber / ProbeResult（跨周期长连接） │
      │  pool.py      AccountPool / AccountHealth（滑动窗口）  │
      │  cleanup.py   CleanupManager（auth 备份/缓存维护）     │
      │  diagnostics  run_diagnostics / Finding（诊断报告）    │
      │  render.py    check/probe 人读渲染（cli 兼容重导出）   │
      └──────────────────────┬───────────────────────────────┘
                             ▼
      ┌──────────────────────────────────────────────────────┐
      │ 基础设施层                                            │
      │  config.py  Config（TOML+ENV+CLI 合并、校验）          │
      │  paths.py   跨平台路径 / opencode 版本探测 / _dedupe   │
      │  headers.py HeaderInjector（官方兼容请求头模板）        │
      │  logs.py    JSON / 人读日志（UTC 时间戳）              │
      │  meta.py    __version__（单一事实来源）                │
      └──────────────────────────────────────────────────────┘
```

模块职责一览：

| 模块 | 职责 | 对外依赖 |
|------|------|----------|
| `meta` | 版本号 | 无 |
| `paths` | OpenCode 候选目录、auth/cache/state 候选路径、`opencode --version` 探测 | platformdirs |
| `config` | 五段配置（daemon/account_pool/prober/headers/cleanup）的加载、合并、校验 | tomllib / tomli-w / platformdirs / paths |
| `headers` | 官方 CLI 兼容头模板（`{version}` 占位、可选 Bearer token） | config |
| `prober` | 异步探测：共享 AsyncClient、状态判定、`error.type` 解析、UTC 午夜估算 | httpx / config |
| `pool` | 账号池：auth 解析（真实 opencode 结构）、三策略轮换、滑动窗口健康度 | config |
| `cleanup` | auth 备份、缓存目录维护 | paths / config |
| `logs` | JSON Lines（真 UTC）与人读日志、Windows GBK 编码防护 | 标准库 |
| `daemon/` | 包：`_runner`（周期编排）/`_lock`（单实例锁）/`_state`（持久化）；公共导入不变 | 上述全部 |
| `render` | `check`/`probe` 人读渲染（trend/events/diff） | daemon（读状态）/ pool |
| `service` | systemd / launchd / Windows 任务计划文件模板 | platformdirs |
| `parser` | argparse 树、结构化命令（banner 抑制）清单 | meta |
| `completions` | 从**真实 parser** 派生 bash/zsh/fish/powershell 补全 | parser / config |
| `diagnostics` | 出口 IP / 代理环境 / 429 分层诊断报告 | httpx / prober / paths / pool |

## 3. 核心数据流

### 3.1 一次性命令（probe / diagnose / quick / …）

```
main()
  → build_parser().parse_args()        # 子命令树；--json/--dry-run 前后皆可（SUPPRESS 技巧）
  → setup_logging(level, json)         # stderr；JSON 行或人读；win32 reconfigure(errors=replace)
  → Config.load(args.config)           # CLI > $OPENCODE_RATE_LIMITER_CONFIG > 平台目录 > 默认
  → COMMAND_HANDLERS[command]()        # 异步处理器，asyncio.run 驱动
       ├─ probe      → HeaderInjector → ModelProber.probe_all（按账号注入 Authorization）
       ├─ diagnose   → 出口 IP/代理/auth 盘点 + 单次探测 + findings 报告
       └─ quick/deep → CleanupManager.full_cleanup（备份 auth [→ 清缓存]）
```

### 3.2 守护进程周期（daemon）

```
run()
  → _load_probe_usage()                # 跨重启恢复每日探测预算计数
  → _acquire_lock()                    # 哨兵 .flock 上 OS 文件锁；被占+存活→拒绝；过期→接管
  → _install_signal_handlers()         # loop.add_signal_handler，win32 回退 signal.signal 桥接
  → loop:
      _probe_cycle():
        0) prober.open() 跨周期长连接      # SIGHUP 按配置变更转移/关闭
        1) 冷却过滤（respect_cooldown）      # 429 过的模型在 retry-after 内跳过
        2) 每日预算裁剪（daily_probe_budget）# 耗尽则整轮跳过
        3) 按策略为每个模型选账号 → 注入 Bearer token
        4) probe_all（共享 AsyncClient 并发）
        5) 结果回写健康度（available=成功；429 由 _handle_rate_limited 记失败+轮换）
        6) 若有 429 且 auto_cleanup_on_429 → **每轮至多一次** full_cleanup
        7) 布防/解除冷却（retry_after 或 UTC 午夜估算）
        8) error_streak（全错连击 → 退避 1×/2×/4×/8×）
        9) pool_health 快照 + history 环形缓冲 + probe_usage 持久化
      _wait(interval × 退避倍数)           # stop/probe 双事件可中断
  → finally: 还原信号 → 写状态 → 释放锁
```

### 3.3 诊断（diagnose）

```
run_diagnostics()
  → collect_proxy_env()                # 纯本地
  → fetch_public_ip()                  # 3 个回显服务回退 + ipaddress 校验（遵循代理环境）
  → fetch_ip_meta()                    # ipinfo.org 归属 enrich（失败降级）
  → inspect_auth_files()               # 存在性/结构/是否含凭证（无secret）
  → prober.probe(单模型)               # 仅 1 次配额
  → _probe_findings()                  # 按 error.type 映射层级行动建议
  → Diagnosis{verdict, exit_code, findings[]}
```

## 4. 关键架构决策记录（ADR 摘要）

| 决策 | 备选 | 选择与理由 |
|------|------|-----------|
| 代码组织：单文件 → 包 | 继续单文件（分发简单） | **拆为 13 模块**。2500 行单文件的 patch 边界、测试隔离、职责演化都已到极限；`__init__` 再导出保证 API 与入口零破坏 |
| 探测客户端 | 每请求一个 AsyncClient | **一轮 probe_all 共享一个池化客户端**。8 模型 × 每轮新建 = 每轮 8 次 TLS 握手；共享后连接复用，`_shared_client` 用后即清防泄漏 |
| 探测频率 | 高频轮询（原 30s） | **900s 默认 + 每日预算 200**。C2：探测与真实用量抢同一份 IP 配额，预算是硬保护而非建议 |
| 429 应对 | 无条件重试 / 本地清锁 | **冷却期（retry-after / UTC 午夜）+ 指数退避 + 每轮至多一次清理**。C1/C3：只有等待与换 IP 有效，工具的职责是别浪费配额 |
| 清理功能 | 删锁/删 state/token 手术 | **诚实化：纯备份 + 缓存维护**。源码核实目标文件不存在，删除虚构文件是伪功能且有害（token 手术会登出用户） |
| 账号池价值 | 宣称免费场景轮换 | **限定付费 key / BYOK 维度**。C4：免费配额键是 IP；文档与诊断均明示 |
| 单实例锁（初版） | 无 / 文件锁库 | O_EXCL 锁文件 + 自研存活检测（v0.4.1 起被哨兵 OS 文件锁取代，见下） |
| 诊断能力 | 只看 HTTP 状态码 | **解析响应体 `error.type`** 分层定位（IP 配额 / key RPM / 上游错误 / 鉴权），配合出口 IP 核实——直接回答"换节点/换账号为什么没用" |
| 补全生成 | 手写脚本 | **从真实 argparse parser 派生**，命令/选项/模型列表单一数据源，永不漂移 |
| 探测连接 | 每轮新建 AsyncClient | **daemon 常驻长连接**（SIGHUP 按配置变更转移/关闭），跨周期 keep-alive；`--once`/CLI 仍用批作用域连接 |
| daemon 规模 | 800+ 行单文件 | **拆为 `_lock`/`_state`/`_runner` 包**，`daemon/__init__` 全量再导出，调用方与测试 patch 点保持兼容 |
| 429 后轮换 | 记失败时再 `get_next` 一次 | **惰性轮换**：归因用 `last_served`，下一轮自然取新账号；round-robin 游标每轮只消费一次 |
| 单实例锁 | O_EXCL + pid 存活检测 | **哨兵文件 OS 文件锁**（进程死亡 OS 自动释放=永不 stale）+ pid 文件信息展示；Windows 字节锁要求哨兵与 pid 分离 |
| 重试 | 无（daemon 层承担） | **`[prober].max_retries` 仅瞬时网络错**，HTTP 状态（含 429）永不重试——429 走冷却，烧配额的重试是 bug |

## 5. 状态与持久化

| 载体 | 位置（platformdirs） | 内容 | 写入方式 |
|------|----------------------|------|----------|
| `config.toml` | 用户配置目录 | 五段配置；由 `generate-config` 生成 | `Config.save()`（tomli-w） |
| `daemon.json` | 用户状态目录 | 运行状态 + `probe_usage`（预算跨重启）+ `cooldowns`（内存态）+ `pool_health` + `history`（环形缓冲） | `.tmp` + `os.replace` 原子替换 |
| `daemon.lock` | 用户状态目录 | 持锁进程 pid | `O_CREAT\|O_EXCL`；释放前校验 pid 为自己 |
| `auth.json.bak` | 与 auth.json 同目录 | `quick`/`deep`/`rotate --apply` 的备份 | `shutil.copy`，内容不变 |

`check` 命令读取 `daemon.json` 并把运行时字段合并进输出（`pool_health` 归位到
`account_pool.health`），形成"配置 × 运行时"的单一视图。

## 6. 与外部系统的契约

| 对端 | 契约 | 失败处理 |
|------|------|----------|
| Zen 网关 `POST /zen/v1/chat/completions` | OpenAI 兼容体；429 带 `retry-after`；错误体 `{error:{type,message}}` | 状态判定 available/rate_limited/error；`error.type` 解析失败→None；超时→error(timeout) |
| IP 回显服务 ×3 | 纯文本 IP | 逐个回退 + `ipaddress` 校验；全败→出口未知 |
| ipinfo.io | JSON（org/country） | 任何失败→空 enrich，不影响诊断 |
| opencode CLI | `--version` 子命令 | 缺失/失败→`unknown`；`OPENCODE_VERSION` 环境变量可覆盖 |
| auth.json | `{provider:{type:"oauth",access,refresh,expires}}` 或 `{type:"api",key}` | 只读盘点/备份；解析失败→shape=unreadable |

## 7. 健壮性架构

- **网络**：所有出站请求带超时；IP 回显逐级回退；诊断外部调用永不抛出（降级为 warn finding）。
- **编码**：win32 下 stdout/stderr `reconfigure(errors="replace")`，GBK 控制台不中断；日志时间戳为真 UTC。
- **并发**：探测 `asyncio.gather`；清理走 `asyncio.to_thread`；信号经 `call_soon_threadsafe` 桥接进事件循环。
- **崩溃面**：状态写失败仅 debug 日志；锁损坏按 stale 接管；诊断对任意畸形响应体安全。

## 8. 测试架构

- **布局**：`tests/` 按主题分文件（core/probe/daemon/completions/build_binary/diagnostics），conftest 提供三类 TOML fixture。
- **隔离约定**：网络 → `pytest-httpx`（注意其响应单次消费语义，周期性探测用 fake probe）；文件系统 → tmp_path + monkeypatch；monkeypatch 目标 = **使用方模块**（如 `opencode_rate_limiter.cli.get_opencode_version`）。
- **质量门**：mypy strict（27 个源文件）、ruff（100 列规则集）、pytest（asyncio auto 模式），CI 矩阵 3 平台 × Python 3.11–3.13 + 二进制构建。
- **计时类测试**：并发性验证用放大 sleep（0.2s）+ 宽松上界（0.35s），容忍 CI 抖动但仍能区分串行/并行。

## 9. 演进方向

具体迭代方案见 [roadmap.md](roadmap.md)（R1 付费 key 轮换强化 → R2 发布 0.3.0 →
R3 趋势与事件 → R4 可靠性收尾 → R5 协议前瞻，含拒绝清单）。
