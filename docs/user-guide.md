# opencode-rate-limiter 使用手册

版本：0.2.0（2026-09） · 场景驱动的操作手册。
命令参数的完整清单与配置字段表见 [MANUAL.md](../MANUAL.md)；功能背景见
[features.md](features.md)；遇到问题想深挖原理见
[implementation.md](implementation.md) 与 [architecture.md](architecture.md)。

---

## 1. 安装

### 1.1 前置要求

- Python 3.11+（或直接使用免安装的二进制，见 §1.3）
- opencode CLI 在 PATH 中（用于探测真实版本号；缺失会回退 `unknown`，
  可用 `OPENCODE_VERSION=xxx` 环境变量手动指定）

### 1.2 源码 / uv

```bash
git clone <repo> && cd opencode-rate-limiter
uv sync --dev                 # 开发环境（含测试/lint/类型检查）
uv run opencode-rate-limiter --help
```

### 1.3 pip 安装

```bash
pip install .                 # 提供 opencode-rate-limiter 命令
opencode-rate-limiter --version
# 可选: HTTP/2 探测支持
pip install ".[http2]"
```

### 1.4 二进制（免 Python 环境）

```bash
pip install ".[build]" && python scripts/build_binary.py
# 产物: dist/opencode-rate-limiter-<platform>-<arch>[.exe]（单文件，约 13 MB）
./dist/opencode-rate-limiter-windows-amd64.exe --version
```

---

## 2. 五分钟上手

```bash
# 1) 看一眼配置与环境是否就绪
opencode-rate-limiter check

# 2) 探测免费模型可用性（8 个模型并发 ping，每个 1 次请求）
opencode-rate-limiter probe

# 3) 被限流了？一条命令定位问题
opencode-rate-limiter diagnose

# 4) 需要长期盯梢时，启动守护进程（默认 15 分钟一轮、每日 200 次预算）
opencode-rate-limiter daemon
```

> **配额须知**：Zen 免费层按**出口 IP** 统计每日请求数（约 200–600 次/天，
> UTC 午夜重置 = 北京时间早 8 点）。`probe` 与 `daemon` 的每次探测都计入同一份
> 配额——工具默认的保守间隔与每日预算就是为了不挤占你的真实使用额度。

---

## 3. 典型场景

### 场景 A：收到 429，我该怎么办？

```bash
opencode-rate-limiter diagnose
```

按报告的 `error.type` 对号入座：

| error.type | 含义 | 你能做的 |
|------------|------|----------|
| `FreeUsageLimitError` | 出口 IP 的**每日配额**用尽 | 等 UTC 午夜重置（报告给出精确本地时刻）；或更换**出口 IP**。**换账号没有用**（配额键是 IP 不是账号） |
| `RateLimitError` | 付费 key 超 1000 RPM | 等约 1 分钟 |
| `server_error` | 网关/上游错误 | 与你无关，稍后重试或换模型 |
| `AuthError` / `MonthlyLimitError` 等 | 账号/计费层 | 检查凭证或订阅，与 IP 无关 |
| 探测未达网关 | 网络/代理链路问题 | 见场景 B |

### 场景 B：我是 Clash 用户，为什么换了节点还限流？

按顺序排查（`diagnose` 会替你完成大部分）：

1. **确认 opencode 的流量真的走了代理**。终端 CLI 不读 Windows"系统代理"：
   - 要么设置环境变量后启动 opencode：
     ```bash
     export HTTPS_PROXY=http://127.0.0.1:7897   # bash
     set HTTPS_PROXY=http://127.0.0.1:7897      # cmd
     ```
   - 要么开 Clash 的 **TUN 模式**接管全局。
   - 验证：`curl https://api.ipify.org` 开/关代理各跑一次，输出必须不同。
2. **确认出口 IP 就是节点 IP**：`diagnose` 报告的"当前出口 IP"若不是你选的节点，
   说明代理对该流量未生效（检查 NO_PROXY 是否包含 opencode.ai）。
3. **理解共享节点**：机场节点是多人共用的——200–600 次/天的 IP 配额可能早被其他
   用户耗尽。换**不同地区/不同服务商**的节点才有意义。
4. **IPv6 注意**：网关按 /64 前缀聚合，同网段换 IPv6 地址无效（diagnose 会标出
   你的 /64 前缀）。
5. 以上都对还限流 → 可能撞上**全体用户共享的模型 TPM**（分钟级拥堵），
   等一分钟，或换模型。

### 场景 C：我有多个付费 Zen key / BYOK 凭证，想轮换使用

1. 生成配置并编辑账号池：

   ```bash
   opencode-rate-limiter generate-config
   # 编辑 config.toml:
   # [account_pool]
   # strategy = "health"
   # accounts = [
   #   { name = "key-1", auth_json = '{"type":"api","key":"sk-..."}' },
   #   { name = "key-2", env_var = "OPENCODE_KEY_2" },     # 从环境变量读
   #   { name = "key-3", auth_path = "~/keys/backup.json" },
   # ]
   ```

2. 验证与切换：

   ```bash
   opencode-rate-limiter rotate --dry-run          # 预览将选中谁、token 能否解析
   opencode-rate-limiter rotate --strategy round_robin
   opencode-rate-limiter rotate --apply            # 真正写入 auth.json（自动备份）
   ```

3. 让健康度参与决策：跑 `probe`/`daemon` 后，探测结果会回写各账号的健康度
   （成功率/延迟/近期错误，近 100 次滑动窗口），`health` 策略据此择优。
   `check` 可查看各账号健康快照。

> 免费模型匿名可用且按 IP 限额——账号池在免费场景下不增加额度（`diagnose`
> 遇到 `FreeUsageLimitError` 时会再次提醒）。它的价值在付费 key 维度。

### 场景 D：后台长期运行

```bash
# systemd（Linux 用户级）
opencode-rate-limiter generate-systemd > ~/.config/systemd/user/opencode-rate-limiter.service
systemctl --user daemon-reload && systemctl --user enable --now opencode-rate-limiter

# macOS launchd
opencode-rate-limiter generate-launchd > ~/Library/LaunchAgents/com.opencode.ratelimiter.plist
launchctl load ~/Library/LaunchAgents/com.opencode.ratelimiter.plist

# Windows 计划任务
opencode-rate-limiter generate-task > task.xml    # 见 MANUAL §6.5 的 schtasks 步骤
```

运行期管理：

```bash
kill -USR1 <pid>   # 立即触发一轮探测
kill -HUP  <pid>   # 重载配置（不丢探测预算计数与账号健康度）
kill      <pid>    # 优雅停止
opencode-rate-limiter check    # 查看运行状态/预算/冷却/健康度
```

**预算建议**：daemon 与真实用量共享每日 IP 配额。默认 `interval_seconds=900` +
`daily_probe_budget=200` 已很克制；若你当天重度使用模型，把预算降到 50–100，
或只在需要时临时启动 daemon。

### 场景 E：就想清理一下本地状态

```bash
opencode-rate-limiter quick --dry-run   # 预览：备份 auth.json（内容不变）
opencode-rate-limiter quick             # 执行备份 + 打印配额提示
opencode-rate-limiter deep              # quick + 清空缓存目录
```

> 诚实说明：opencode **没有**本地限流状态可供"解除"，`quick/deep` 只做安全的
> 备份与缓存维护；服务端限额只能等重置或换出口 IP（见场景 A/B）。

---

## 4. 配置速查

配置文件位置（也可用 `--config` 或 `OPENCODE_RATE_LIMITER_CONFIG` 指定）：

| 平台 | 路径 |
|------|------|
| Windows | `%LOCALAPPDATA%\opencode-rate-limiter\opencode-rate-limiter\config.toml` |
| Linux | `~/.config/opencode-rate-limiter/config.toml` |
| macOS | `~/Library/Application Support/opencode-rate-limiter/config.toml` |

最常用的几个字段（完整表见 MANUAL §5）：

```toml
[daemon]
interval_seconds = 900         # 探测间隔（探测与真实用量共享配额，勿调太小）
daily_probe_budget = 200       # 每日探测总数硬上限（0 = 不限）
respect_cooldown = true        # 429 的模型在冷却期内跳过探测
history_size = 20              # check 中趋势统计的轮数

[prober]
proxy = "http://127.0.0.1:7897"  # 不设则遵循 HTTP(S)_PROXY 环境变量
# http2 = true                    # 需 pip install ".[http2]"

[account_pool]
strategy = "health"
health_window = 100            # 健康度滑动窗口
score_weights = { success = 0.5, latency = 0.3, recency = 0.2 }
```

环境变量逐字段覆盖：`OPENCODE_RATE_LIMITER_<段>__<键>`，如
`OPENCODE_RATE_LIMITER_DAEMON__INTERVAL_SECONDS=1800`。

---

## 5. 退出码

| 码 | 含义 |
|----|------|
| 0 | 成功（`diagnose`=健康） |
| 1 | 业务失败：清理出错 / `probe` 有模型被限 / `rotate` 无账号或凭证不可解析 / `diagnose` 被限流 / daemon 已有实例 |
| 2 | 配置错误（TOML/校验）/ argparse 用法错误 / `diagnose` 网络层失败 |
| 130 | Ctrl+C 中断 |

---

## 6. 故障排查 FAQ

**Q1：换了 Clash 节点还是 429？**
九成是流量没走代理（CLI 不读系统代理，见场景 B 第 1 步）；其次是共享节点 IP 的
日配额被他人耗尽、IPv6 /64 聚合、或全局 TPM 拥堵。跑 `diagnose` 看出口 IP 与
`error.type` 即可定位。

**Q2：换账号能继续用免费模型吗？**
不能。免费配额按 IP 统计，账号不参与。账号池只在付费 key/BYOK 维度有真实收益。

**Q3：`quick`/`deep` 能"解除限流"吗？**
不能。它们只做 auth 备份与缓存维护（opencode 本地本就没有限流状态）。
服务端配额只能等 UTC 午夜重置或更换出口 IP。

**Q4：daemon 报"Daily probe budget exhausted"？**
当日探测预算（默认 200）用尽，属预期保护行为，UTC 午夜自动恢复；可用
`check` 查看用量，或调大 `daily_probe_budget`（注意挤占真实额度）。

**Q5：探测全是 error(timeout)？**
网络/代理问题：检查代理端口是否监听、节点是否可用、`HTTPS_PROXY` 是否设置；
或调大 `[daemon].probe_timeout_seconds`。

**Q6：`check` 显示的 daemon 信息是旧的/没有？**
`daemon.json` 由 daemon 进程写入；没跑过 daemon 就没有运行时字段。
多个 daemon 不能并存（单实例锁），死进程遗留的锁会自动接管。

**Q7：重载配置（SIGHUP）会丢什么？**
不会丢探测预算计数与账号健康度；会应用新的 interval/models/账号列表等配置。
CLI 显式给的 `--interval/--models` 覆盖在重载后依然保留。

**Q8：哪里看"某模型最近一段时间稳不稳"？**
`check` 的 `probe history` 段（各模型近 N 轮的状态计数，N=`history_size`）。

---

## 7. 文档地图

| 文档 | 用途 |
|------|------|
| 本手册（user-guide.md） | 安装、场景化操作、FAQ |
| [MANUAL.md](../MANUAL.md) | 命令参数与配置字段的**权威完整参考**（含实现差异核对） |
| [features.md](features.md) | 每个功能的定位、行为细节与能力边界 |
| [implementation.md](implementation.md) | 实现细节（配置合并/探测/daemon 流水/锁/诊断） |
| [architecture.md](architecture.md) | 架构、数据流、设计决策与外部契约 |
| [roadmap.md](roadmap.md) | 迭代路线图（R1–R5） |
| [CHANGELOG.md](../CHANGELOG.md) | 变更记录 |
