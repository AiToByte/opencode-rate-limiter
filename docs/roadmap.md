# opencode-rate-limiter 迭代路线图（Roadmap）

版本：2026-09 · 基线：v0.4.1（正确性收尾：文件锁/归因/合并/超时细分）
配套：[architecture.md](architecture.md) · [features.md](features.md) ·
[implementation.md](implementation.md) · [user-guide.md](user-guide.md) ·
[MANUAL.md](../MANUAL.md)

---

## 0. 规划原则

1. **不做滥用规避**：不实现多 IP 轮换、代理池、账号 farming 等以绕过服务端限额为
   目的功能——既违反 ToS，也与本工具"配额保护"的定位冲突（见 architecture.md C1）。
2. **每轮可独立发布**：一轮 = 一组可验证的闭环功能，测试全绿 + 文档同步即可提交，
   不做跨轮半成品。
3. **向后兼容**：配置字段只增不删；行为变更必须在 CHANGELOG 标注并更新四份文档。
4. **探测不挤占真实用量**：任何新增的自动网络行为都必须纳入预算/冷却框架审查。

## 1. 迭代总览

| 迭代 | 主题 | 用户价值 | 规模 | 优先级 | 依赖 |
|------|------|----------|------|--------|------|
| R1 | 付费 key / BYOK 轮换强化 | 账号池在真实计费维度可用 | 中 | **P0** | 无 |
| R2 | 发布工程（Phase 7） | 产出可分发的 0.3.0 | 小 | **P0** | 无（建议在 R1 后） |
| R3 | 可观测性：趋势与事件 | 一眼看清"最近几小时稳不稳、发生过什么" | 中 | P1 | 无 |
| R4 | 可靠性收尾 | 清理实现层面的已知毛边 | 小 | P1 | 无 |
| R5 | 协议前瞻 | 为网关行为回归做准备 | 小 | P2 | R1 |

建议节奏：R1 → R2（发 0.3.0）→ R3 → R4 → R5。R2 也可提前单独执行。

---

## 2. R1 — 付费 key / BYOK 轮换强化（P0）

### 目标

账号池的当前实现只解析 OAuth 的 `access` 令牌；API key 形态
（`{"type":"api","key":...}`，keyRateLimiter 1000 RPM/key 的主体）解析不到、
轮换写回的 auth.json 形态不完整、限流归因不区分"IP 配额"与"key RPM"。
本迭代把账号池补齐到它的真实用武之地。

### 任务分解

**R1.1 凭证抽象（pool.py）**
- 新增 `extract_credential(auth) -> tuple[kind, token] | None`：
  `kind ∈ {"oauth", "api"}`；oauth 取 `access`/`access_token`，api 取 `key`。
  `extract_access_token` 保留为 oauth 兼容别名。
- `Account` 增加 `kind` 运行时字段（推断自 auth 结构，可被配置显式覆盖）。
- `resolve_token` → `resolve_credential`（内部沿用旧名作别名，保持 API 兼容）。

**R1.2 注入与归因（prober.py / cli.py / daemon.py）**
- 探测注入：api 类凭证同样以 `Authorization: Bearer <key>` 发送（与网关
  `parseApiKey` 的 `authorization.split(" ")[1]` 对齐）。
- 限流归因：`ProbeResult.error_type` 已有；在 `pool.mark_result` 增加 `error_type`
  参数——`RateLimitError` 记为 **key 维度失败**（新字段
  `key_limited_count`），`FreeUsageLimitError` 不计入账号健康度（那是 IP 的事，
  与凭证无关）。
- `_handle_rate_limited`：`RateLimitError` 时对该 key 短冷却（60s），不触发
  账号轮换之外的全局动作；`FreeUsageLimitError` 维持现状。

**R1.3 rotate 写回形态（cli.py）**
- `rotate --apply` 对 api 类账号写正确的 provider 键控结构
  `{"https://opencode.ai/zen": {"type":"api","key":...}}`（oauth 类维持整快照写回）。

**R1.4 脱敏展示（cli.py / diagnostics.py）**
- 新增 `_fingerprint(credential) -> "sk-abc…x9f4"`（前 6 后 4）；`check` 的
  `account_pool`、`rotate` 输出、诊断凭证盘点统一展示指纹而非长度布尔。

**R1.5 测试与文档**
- 新增：三类 auth 形态的 credential 提取、RateLimitError 归因不计 IP 配额、
  api 账号 apply 写回形态、指纹脱敏。
- 更新：features.md §6（账号池能力边界改写为"两种凭证维度"）、MANUAL §5.3、
  §4.6、user-guide 场景 C、CHANGELOG。

### 验收标准

- `rotate --apply` 写出的 auth.json 能被 opencode CLI 以 api key 正常读取
  （手工冒烟：`opencode` 使用免费+付费模型各一次）。
- 混合 oauth/api 账号池 `probe`：请求按账号携带正确凭证；429 时健康度归因正确
  （RateLimitError→key 失败；FreeUsageLimitError→不影响健康度）。
- 所有输出（含 `--json`）中 key 明文不可见，指纹格式统一。
- 测试 ≥ +12 条，mypy/ruff 全绿。

### 风险与缓解

- 网关对 api key 认证头的确切格式仅由 `parseApiKey` 推断（`Bearer` 前缀假设）→
  用真实 key 手工冒烟一次；失败则降级为"仅 oauth 注入"并在文档标注。
- auth.json provider 键的确切字面量（`https://opencode.ai/zen`）以官方文档/实测
  为准，实现为常量便于调整。

---

## 3. R2 — 发布工程（Phase 7，P0）—— 已于 v0.3.0 实现（release.yml + docs/release.md）

### 目标

把当前 0.2.0+ 的积累发成 **0.3.0**：GitHub Release 三平台二进制 + PyPI 包。

### 任务分解

**R2.1 版本定版**
- `meta.py` / `pyproject.toml` / `man` 页三处版本号 → 0.3.0（有 §11.3 核对清单）。
- CHANGELOG：`[Unreleased]` 内容归并为 `[0.3.0]`（含本轮所有破坏性标注：
  默认间隔 900s、清理语义、`rotate_auth_tokens` 更名）。

**R2.2 发布流程重建（`.github/workflows/release.yml` 或复用 ci.yml 的 release job）**
- tag `v*` 触发：三平台 build_binary.py（已实测可用，含入口 shim）→ 产物 +
  SHA256SUMS → GitHub Release。
- 新增 PyPI 发布 job：`uv build` + PyPI trusted publishing（或 token），
  `pip install opencode-rate-limiter` 冒烟。
- 发布前检查 job：三处版本号一致性断言（脚本化，防手工遗漏）。

**R2.3 发布文档**
- 重建 `docs/release.md`：tag 命名、检查清单（测试/版本/CHANGELOG/文档同步）、
  发布后验证（Release 资产齐全、PyPI 页面、`uvx` 冒烟）。

### 验收标准

- `git tag v0.3.0 && git push --tags` 后全自动产出：3 个二进制 + SHA256SUMS +
  PyPI 版本；`uvx opencode-rate-limiter --version` 可用。
- 全流程无手工步骤（除打 tag）。

### 风险与缓解

- PyPI 名占用/审核：先查询占位，必要时用 trusted publishing 试发 dev 版。
- Windows 构建偶发超时：CI 已有重试余量（fail-fast=false）。

---

## 4. R3 — 可观测性：趋势与事件（P1）

### 目标

让"最近几小时稳不稳、系统做过什么决策"脱离 `check` 的计数快照，变成可读的趋势
与审计轨迹。

### 任务分解

**R3.1 趋势网格（cli.py 或新 `history` 子命令）**
- `check --trend`（或 `opencode-rate-limiter history`）：按轮输出状态字母网格，
  如 `deepseek  a a a ! a x`（a=available, !=rate_limited, x=error, .=cooldown
  skipped），一眼看出抖动区间。

**R3.2 事件审计环（daemon.py）**
- 新增 `events` 环形缓冲（复用 history 的 deque 模式）：记录
  rotation / cleanup / budget-exhausted / cooldown-armed / reload 等决策事件
  （时间 + 类型 + 主体），随 `daemon.json` 持久化，`check --json` 与趋势视图展示。

**R3.3 probe 对比输出（cli.py）**
- `probe` 人读输出在存在 daemon 状态文件时附一行"与上次相比"的变化
  （新限流模型 / 恢复模型），帮助即时判断。

### 验收标准

- 趋势网格在 20 轮历史下宽度可控（截断 + 汇总）；
- 事件环上限可配、持久化往返无损；
- 新增测试 ≥ 10 条；文档四处同步。

### 风险

- daemon.json 体积增长：events/history 均有上限，无需迁移。

---

## 5. R4 — 可靠性收尾（P1）—— 已于 v0.3.0 实现

来自 implementation.md §10 与 MANUAL §13 的遗留毛边，逐项小改动：

| # | 任务 | 模块 | 说明 |
|---|------|------|------|
| R4.1 | 冷却期跨重启持久化（存绝对 UTC 时刻而非剩余秒） | daemon | 重启后不再对已限流模型空探一轮 |
| R4.2 | 嵌套表深合并（`extra_headers` 等嵌套表不再被字符串化；部分 `score_weights` 因与"和为 1"校验冲突而被明确拒绝） | config | 深合并实现于 `_merge_value` |
| R4.3 | Windows 任务 XML 按声明编码（UTF-16）输出或改声明为 UTF-8 | service | 消除 schtasks 导入歧义 |
| R4.4 | `headers` 模板支持 `{model}` 占位符 | headers | 低频需求，顺手 |
| R4.5 | NO_PROXY 与目标域匹配的精确判定（当前为条件性提示） | diagnostics | 用 `urllib.proxy_bypass`/自实现匹配 |

验收：各自带回归测试；MANUAL §13 相应条目更新。

---

## 6. R5 — 协议前瞻（P2）

**触发条件驱动**，不定期执行：

- **头检查回归预案**：网关 `checkHeaders` 当前被注释禁用。R1 完成后本工具已能
  携带真实凭证；再补一步——`Subscription.getFreeLimits().checkHeaders` 若在
  线上生效（表现为"带头可用/不带头降额"），把 `dailyRequestsFallback` 语义
  写回诊断与文档。**验证方法**：diagnose 已能区分限流层级，无需盲改。
- **`x-model` 探测头**：探测请求是否加 `x-model` 以贴近官方客户端行为（观测项，
  当前网关仅打点）。
- **HTTP/2 端到端验证**：`[prober].http2` 已实现 + h2 extra，做一次真实端点验证
  并记录结论到 MANUAL。
- **模型清单自动化**：`zen/v1/models` 端点若公开无鉴权，`probe all` 可自动发现
  新免费模型并提示更新 `FREE_MODELS`（保守：只提示不自动改配置）。

---

## 7. 明确不做（拒绝清单）

| 方向 | 理由 |
|------|------|
| 多 IP / 代理池轮换以规避日配额 | ToS 滥用；且与"配额保护"定位冲突 |
| 账号 farming / 批量注册管理 | 同上 |
| 模拟官方客户端头以"提升"免费额度 | 头检查已禁用且属规避行为；`headers` 功能定位为保险与信息展示 |
| 后台自动改写用户 opencode 配置（非显式 `--apply`） | 破坏用户知情权 |
| token/配置加密存储 | 单机 CLI 场景收益低，密钥管理引入的复杂度远超收益（旧规划已证伪项） |

---

## 8. 依赖与排期建议

```
R1 (key 轮换) ──► R2 (发布 0.3.0) ──► R3 (趋势/事件) ──► R4 (收尾)
                                        └──────────────► R5 (前瞻, 触发式)
```

- R1、R2 无相互依赖，但建议 R1 先行：0.3.0 的发布说明将包含"付费 key 轮换"这一
  完整故事，比发布后再补更有价值。
- R3/R4 可并行；R5 按网关线上行为触发，不占常规排期。
- 每轮结束的固定动作：CHANGELOG 归并、四份文档 + MANUAL 同步、全量
  pytest/ruff/mypy、补全脚本重新生成、git 提交（本轮既定流程）。

---

## 9. 已落地轮次（0.4.x / 0.5.0，严格 semver）

### v0.4.1 — A 轨正确性收尾（patch）

- 单实例锁改 OS 文件锁（哨兵 `.flock` + pid 文件信息展示），消 TOCTOU 双跑
- 轮换归因改 `last_served`，429 处理不再双消费 round-robin 游标
- `kind` 取值校验、递归深合并、大小写不敏感字段匹配
- dry-run 计数诚实化（`would_clear_count`）、超时三细分、错误体 64KB 上限
- `probe_all` 重入守卫、NO_PROXY 纯函数化、版本缓存测试隔离
- man 页勘误（state.json/退出码表）、release 测试门补 lint/type

### v0.5.0 — B 轨架构 + D 轨逃生三角（minor）

- 探测连接跨周期复用（daemon 持有长连接，SIGHUP 按配置变更决定转移/关闭）
- `daemon.py` 拆为 `daemon/` 包（`_lock` / `_state` / `_runner`），公共导入不变
- auth 读取 mtime+size 感知缓存（`invalidate_auth_cache()` 供测试/轮换）
- `[prober].max_retries`（仅瞬时网络错，永不重试 429）
- `daemon --once`（单轮巡检，cron 友好，退出码沿 probe 语义）
- `rotate --to NAME`（故障逃生直切，`explicit` 进 JSON/日志）
- `daemon --stop`（SIGTERM + 10s 等待，跨平台；自锁拒绝防自杀）
