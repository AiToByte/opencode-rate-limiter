# opencode-rate-limiter 功能详细介绍文档

版本：0.5.0（2026-09） · 本文回答"每个功能是什么、解决什么问题、边界在哪"。
命令参数的逐项清单见 [MANUAL.md](../MANUAL.md)；实现原理见
[implementation.md](implementation.md)。

---

## 1. 前提：OpenCode Zen 免费层的真实限额（功能设计的事实基础）

以下结论经 opencode 网关源码核实（`packages/console/app/src/routes/zen/`），
是全部功能设计的出发点：

1. **免费层按出口 IP 统计每日请求数**（Redis，UTC 自然日重置，社区实测约
   200–600 次/天；新 IP 前 7 天额度 ×2）。账号不参与免费配额。
2. **429 携带 `retry-after` 头**，值 = 距 UTC 午夜的秒数（北京早 8 点重置）。
3. 付费 Zen key 按 key+模型 **1000 RPM**；另有全体用户共享的模型 TPM（分钟级拥堵）
   与试用 token 长期预算（按 IP 累计）。
4. **官方客户端头检查当前被网关注释禁用**（头仅用于打点）。
5. **opencode CLI 没有本地限流状态**——没有 state.json、没有限流锁、auth.json 里
   没有 rate_limited 字段。

由此推出本工具的三条功能主线：**看清楚（探测/诊断/健康检查）、不浪费（预算/冷却/
退避）、管理好多凭证（付费 key 维度的账号池）**——以及一条明确的"不做什么"：
本地操作不能解除服务端限额。

功能总览：

| 功能 | 一句话 | 关键命令 |
|------|--------|----------|
| 模型探测 | 并发 ping 免费模型，判定可用/限流/错误并解析错误层级 | `probe` |
| 限额诊断 | 出口 IP + 代理 + 错误分层 + 建议，一条命令定位"为什么被限" | `diagnose` |
| 守护进程 | 保守节奏的自动探测 + 冷却 + 预算 + 退避 + 状态持久化 | `daemon` |
| 健康检查 | 配置 × 运行时状态聚合输出 | `check` |
| 账号池 | 多凭证轮换与健康管理（付费 key/BYOK 维度有效） | `rotate` |
| 本地维护 | auth 备份 + 缓存清理（真实、无害） | `quick` / `deep` |
| 请求头 | 官方 CLI 兼容头导出（机制回归时的保险） | `headers` |
| 配置引导 | 生成带注释的默认配置 | `generate-config` |
| 服务部署 | systemd / launchd / Windows 计划任务文件 | `generate-*` |
| Shell 补全 | bash/zsh/fish，从真实 parser 派生 | `completions` |

---

## 2. 模型探测（`probe`）

**解决什么**：在消耗极少量配额的前提下，知道"某个免费模型现在能不能用、被限到
什么时候"。

**行为**：向 `https://opencode.ai/zen/v1/chat/completions` 发一条 `max_tokens=1`
的 ping。判定矩阵：

| HTTP | 结果 | 附加信息 |
|------|------|----------|
| 200 | `available` | 延迟 |
| 429 | `rate_limited` | `retry_after`（真实头）与 `estimated_reset`（缺失时=距 UTC 午夜秒数）、`error_type` |
| 其他 | `error` | `error_type`（若响应体是 Zen 错误格式，如 `server_error`）或网络错误文本 |

**特点**：
- 并发执行（共享一个池化连接的 AsyncClient；daemon 中跨周期常驻）。
- 超时三细分：`connect failed`（链路/代理）/ `connect timeout` / `read timeout`
  （网关慢），排障方向不同；`[prober].max_retries` 只对这类瞬时错即时重试，
  HTTP 状态（含 429）永不重试。
- 配置了账号池时按策略为每个模型注入该账号的 `Authorization: Bearer`，
  并把结果回写健康度——轮换是否生效在输出里直接可见
  （`[account: primary]` 标签 / JSON `account` 字段）。
- 退出码：任一模型限流 → 1；全部可用 → 0。可直接用作脚本探针。
- **注意**：每次探测计入同一 IP 的每日配额（这正是 daemon 预算机制存在的原因）。

## 3. 限额诊断（`diagnose`）

**解决什么**：遇到 429 时，一条命令回答"到底哪一层在限我、我能做什么、做了有没有用"。
这是本工具对"换节点/换账号为什么没用"这类问题的直接回应。

**一次诊断的五个环节**：

1. **环境**：opencode 版本（`OPENCODE_VERSION` 可覆盖）；代理环境变量
   `HTTP(S)_PROXY`/`ALL_PROXY`/`NO_PROXY`（大小写都查）。
2. **出口 IP**：三个回显服务回退 + `ipaddress` 严格校验；经 ipinfo 补充归属
   （org/国家）；IPv6 自动给出 /64 前缀并解释聚合规则。**有意走与探测相同的代理
   路径**，所以报告的出口就是"请求实际用的出口"。
3. **凭证盘点**：各 auth.json 候选的存在性/结构（single-entry / provider-keyed /
   unreadable）/ 是否含凭证；**绝不输出凭证值**。并明确提示：免费模型匿名可用，
   账号不影响免费额度。
4. **单次探测**：只探一个模型（默认配置列表第一个，`--model` 可指定），只花 1 次
   配额。
5. **结论与建议**：`findings[]`（severity: ok/info/warn/fail，每条带 detail 与
   remedy）。典型映射：
   - `FreeUsageLimitError` → IP 日配额用尽；给出 UTC 午夜重置的精确本地时刻；
     **"换账号对此无效"**；"确认出口 IP 与节点切换是否生效"核验清单
     （CLI 不读系统代理、共享机场节点可能已被耗尽、IPv6 /64 聚合）。
   - `RateLimitError` → 付费 key RPM，等一分钟。
   - `server_error` → 网关/上游错误，与配额无关。
   - 请求未达网关（超时/拒连）→ 网络/代理链路排查建议。

**健壮性**：所有外部调用带超时、逐级降级为 warn finding；任何异常都不会让诊断崩溃；
退出码 0/1/2 = 健康/被限流/网络错误，可脚本化。

## 4. 守护进程（`daemon`）

**解决什么**：长期、克制地盯着模型可用性，且**绝不把自己变成配额黑洞**。

四个保护机制协同：

1. **保守节奏**：默认 900s 一轮（探测与真实用量抢配额，节奏是保护措施）。
2. **每日预算**：`daily_probe_budget`（默认 200 次/UTC 日）硬上限，计数跨重启
   持久化；预算内按需裁剪当轮模型数；耗尽后跳过周期并在 `check` 里可见
   （`probe budget: 37/200 used`）。
3. **每模型冷却**：429 的模型按 `retry-after`（或 UTC 午夜估算）进入冷却，期内
   跳过探测——不反复撞已知限流的模型；恢复即解除；`respect_cooldown=false` 可关。
4. **错误退避**：整轮全 error（网络故障）时等待间隔 1×→2×→4×→8×（封顶）指数放大，
   恢复即复位。

**附带能力**：
- **账号轮换**：429 时对"服务该模型的账号"记失败并轮换（免费场景仅影响选择与
  日志；付费 key 场景有真实意义）。
- **自动清理**：出现 429 且 `auto_cleanup_on_429=true` 时，每轮**至多一次**
  auth 备份 + 缓存维护（不再对每个限流模型各触发一遍）。
- **可观测**：`daemon.json` 持久化运行状态、探测历史环形缓冲、**决策事件审计环**
  （默认 50 条——每次冷却布防、key 冷却、账号轮换、自动清理、预算耗尽、配置重载
  都有迹可循）、账号健康快照、预算与冷却。
- **趋势网格**（R3）：`check --trend` 以状态字母矩阵展示各模型近 N 轮的可用性
  演变，抖动区间一眼可见。
- **信号控制**：SIGTERM/SIGINT 优雅停止；SIGHUP 重载配置（CLI 覆盖与账号健康度
  跨重载保留；探测配置不变时**复用暖连接**，变了才重建）；SIGUSR1 立即探测；
  SIGUSR2 打印状态。Windows 走 stdlib 信号桥接。
- **单实例锁**：哨兵 `.flock` 上的 OS 文件锁互斥 + `daemon.lock` 记 pid；
  第二个实例明确报错退出（退出码 1），死进程遗留锁自动接管（v0.4.1 起无竞态）。
- **长连接复用**（v0.5.0）：连接池 client 跨周期常驻，告别每轮 TCP/TLS 重建。
- **单轮模式**（v0.5.0）：`daemon --once` 跑一轮就退出（拿锁、防并发 daemon），
  退出码沿 `probe` 语义（有 rate_limited 即 1）——给 cron/任务计划用。
- **优雅停止**（v0.5.0）：`daemon --stop` 读锁发 SIGTERM 并等最多 10s；
  无锁/过期锁/自锁（防自杀）各有明确退出行为。

## 5. 健康检查（`check`）

**解决什么**：一条命令看全"配置是什么 + daemon 现在怎么样"。

- 人读摘要（默认）：opencode 版本、daemon 配置与运行时（含 pid/cycles/uptime）、
  冷却剩余、探测预算用量、账号池与健康度（success/total/failures/avg/score，
  凭证以指纹展示）、最近一轮探测结果、探测历史趋势（各模型近 N 轮状态计数）、
  路径计数。`--trend` 追加趋势网格与最近决策事件。
- **probe diff**（R3）：`probe` 人读输出在存在 daemon 历史时自动附加「与上一轮
  相比」——新增限流/已恢复的模型，即时判断波动方向。
- `--json`：完整机器可读报告（含 `pool_health` 归位到 `account_pool.health`）。
- 退出码恒 0（状态查询，不承担判定）。

## 6. 账号池（`rotate`）

**解决什么**：管理**多个付费 Zen key / BYOK 凭证**时的选择、轮换与健康维护。

- 三种策略：`round_robin`（轮询）、`least_used`（用得最少者优先）、
  `health`（评分最高者优先）。
- 健康评分（权重可配 `[account_pool].score_weights`，默认 成功 0.5/延迟 0.3/新旧 0.2）：
  成功率来自**滑动窗口**（默认近 100 次结果）+ 延迟 EMA + 错误新旧。
  探测/daemon 的结果会回写，策略随真实表现演化。
- **双凭证形态**（R1）：oauth（`access`/`access_token`）与 api key
  （`{"type":"api","key"}`，含 provider 键控与裸 key 形态）均可解析与注入；
  `check`/`rotate` 以**指纹**（前 6 后 4，如 `sk-abc…wxyz`）展示凭证，明文绝不输出。
- **限流归因**（R1）：`RateLimitError`（key RPM）计入该账号的 key 维度失败计数
  （`key_limited_count`）并触发 key 冷却（`[daemon].key_cooldown_seconds`，默认
  60s）——冷却中的账号不再注入凭证，
  全部冷却时回退匿名头；`FreeUsageLimitError`（IP 配额）**不**计入凭证健康度，
  因为那是 IP 的责任而非凭证的。
- `rotate --apply`：把选中账号的凭证**归一化**写入活动 auth.json（`build_auth_payload()`）：
  api key 写为 `{zen键: {type:api, key}}`，单条 oauth / bare 载荷包裹为 provider
  键控结构，完整快照原样透传；先备份 `.json.bak`；`--dry-run` 只预览目标。
  账号无可解析凭证时报错（退出码 1）。
- `rotate` 输出包含 `auth_token_resolved` 与 `credential`（kind + 指纹）——
  token 解析支持 opencode 真实 auth 结构（`{type:"oauth", access}`、
  `{type:"api", key}` 与 provider 键控 map）。
- `rotate --to NAME`（v0.5.0）：故障逃生直切指定账号，跳过策略；JSON/日志带
  `explicit` 标记；未知名报错并列出可用账号（退出码 1）。

**能力边界（重要）**：免费模型配额键是 IP，**同 IP 换账号不增加额度**；账号池的
真实收益在付费 key 维度（每 key 独立 1000 RPM）。诊断命令会在遇到
`FreeUsageLimitError` 时主动重申这一点。

## 7. 本地维护（`quick` / `deep`）

**解决什么**：安全的本地维护动作 + 诚实的预期管理。

- `quick`：把所有候选 auth.json **原样备份**为 `.json.bak`（内容不变——早期版本
  清空 token 的手术已移除：对服务端限额无效且会登出用户），并输出配额提示
  （服务端按 IP/UTC 日计数、本地操作不能解除、距重置还有多久）。
- `deep` = quick + 清空缓存目录（递归删除后重建空目录；目录来自配置
  `[cleanup].cache_dirs` 与 OpenCode 原生候选，存在的才处理）。
- 两者都支持 `--dry-run` 与 `--json`；退出码 1 表示有操作失败。
- **不做的事**：不删任何"限流锁/state.json"（它们不存在），不清 token。

## 8. 请求头（`headers`）

**解决什么**：生成与官方 opencode CLI 一致的请求头，供第三方客户端直连 Zen 时使用。

- `headers [--model M] [--export]`：JSON 或 `export KEY="value"` 形式；
  含 `User-Agent`/`x-opencode-client`/`x-opencode-version`（模板仅支持 `{version}`
  占位，版本来自 `opencode --version`，可用 `OPENCODE_VERSION` 覆盖）与
  `Content-Type`/`Accept`，指定 `--model` 时追加 `x-model`。
- **当前效力**：网关的头检查已被注释禁用（`headersExist = true` 硬编码），这些头
  暂不影响限额——保留作为该机制回归时的低成本保险，输出中的版本探测依然真实有用。

## 9. 配置引导与服务部署

- `generate-config [--force]`：把内置默认值（含全部新字段的注释示例）写到平台配置
  目录或 `--config` 路径；已存在需 `--force`，避免覆盖用户配置。
- `generate-systemd` / `generate-launchd` / `generate-task`：三端服务文件模板，
  均通过 `OPENCODE_RATE_LIMITER_CONFIG` 指向默认配置（该变量已被 `Config.load`
  实际读取），内置资源上限（systemd `MemoryMax=100M`/`CPUQuota=10%`）。
- `completions bash|zsh|fish|powershell`：补全脚本由真实 parser 派生——新增子命令/选项/模型
  无需手改补全；`scripts/generate_completions.py` 一键刷新仓库内 `completion/`。

## 10. 横切能力

- **配置系统**：CLI > `$OPENCODE_RATE_LIMITER_CONFIG` > 平台目录 > 默认值；
  环境变量逐字段覆盖（双下划线分层、类型自动强转）；完整校验失败即退出码 2。
- **日志**：stderr 上的人读/JSON Lines 双格式（JSON 时间戳为真 UTC）；`-v/-vv/-q`
  控制级别；Windows GBK 控制台不中断。
- **跨平台**：路径解析覆盖 `~/.opencode`、XDG、macOS Library、Windows
  APPDATA/LOCALAPPDATA；信号在 Windows 降级；进程存活检测双实现。
- **明确不做的事**（诚实边界）：解除服务端限额、按账号提升免费额度、模拟"登录态"
  提升配额——这些在服务端维度不成立，工具的职责是观测、保护配额、管理好你合法
  拥有的多份凭证。
