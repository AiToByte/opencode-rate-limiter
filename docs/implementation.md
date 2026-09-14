# opencode-rate-limiter 技术实现细节文档

版本：0.4.0（2026-09） · 配套：[architecture.md](architecture.md)（为什么） ·
本文讲"具体怎么实现的"，函数/字段名与当前代码一致。

---

## 1. 配置系统（config.py）

### 1.1 加载与优先级

`Config.load(path)` 的解析顺序：

1. `--config` 显式路径；未给时查环境变量 `OPENCODE_RATE_LIMITER_CONFIG`
   （支持 `~`、`$VAR`、`%VAR%`：先 `expandvars` 再 `expanduser`）；仍未给则用
   platformdirs 用户配置目录。
2. TOML → dict（`tomllib`）；语法错误抛 `ValueError` → main 退出码 2。
3. `Config._merge(defaults, file_dict)`，随后同样地合并环境变量层
   （`OPENCODE_RATE_LIMITER_<SECTION>__<KEY>`，双下划线分层，值经
   `_parse_env_value` 推断类型：bool/int/float/逗号 list/str）。
4. `validate()`：各段自带校验（interval≥5、models 非空、health_window≥1、
   score_weights 三键且和为 1、endpoint 必须 http(s)、connection_pool_size≥1、
   preserve_config 强制 true、daily_probe_budget≥0 等），任一失败退出码 2。
5. `_expand_paths()`：cleanup 路径做 `~`/环境变量展开，缓存到私有字段。

### 1.2 合并的类型强转

`_coerce_value(current, new)`：以**默认值/文件值**的类型为模板强转环境变量值——
bool 目标接受 "true/1/yes"；int/float 目标直接 `int(new)`/`float(new)`；
其余（str、list、dict）原样传递。这就是 `PROBER__MAX_TOKENS=2` 能变成整数的原因。
注意嵌套表（如 `score_weights`、`extra_headers`）是**整表替换**而非深合并。

### 1.3 序列化

`Config.save()` 用 tomli-w 写回五段；两个细节：
- `prober.proxy` 为 None 时不写入（tomli-w 不支持 None）。
- 新增字段必须**同时**加进 dataclass 与 save() 字典，否则 round-trip 丢数据
  （有专门的 round-trip 测试守护）。

## 2. 探测器（prober.py）

### 2.1 客户端构建与复用

`_build_client()` 组装 `httpx.AsyncClient`：

- `timeout` 来自构造参数（daemon 用 `daemon.probe_timeout_seconds`）。
- `proxy`：仅当 `[prober].proxy` 设置时传入（httpx ≥ 0.28 的 `proxy=` 参数）；
  未设置时 httpx 默认遵循 `HTTP(S)_PROXY` 环境变量（trust_env）。
- `http2`：`importlib.util.find_spec("h2")` 探测可选依赖；缺失则 log warning 并
  回退 HTTP/1.1（不抛异常）。
- `limits`：`httpx.Limits(max_connections=size, max_keepalive_connections=size)`，
  size = `connection_pool_size`（默认 8）。

**复用协议**：`probe_all()` 建一个客户端 → 置 `self._shared_client` → gather 全部
探测 → `finally` 中清空引用并 `aclose()`。`probe()` 若发现 `_shared_client` 非空
（处于一轮 probe_all 内）直接复用，否则建一次性客户端并关闭。
用 `try/finally` 保证异常路径也不泄漏连接。

**空列表短路**：`probe_all` 在 `models` 为空时直接返回 `[]`——冷却期跳过全部模型
时不会空建客户端。

### 2.2 单次探测与状态判定

`_do_probe(model, headers, client)`：

- 请求体 `{model, messages:[{role:user, content: ping_message}], max_tokens,
  temperature: 0}`；共享头 + `extra_headers` 合并（后者覆盖同名键）。
- 延迟用 `time.monotonic()` 计（不受系统时钟跳变影响），时间戳为 UTC ISO-Z。
- 判定：
  - 200 → `available`
  - 429 → `rate_limited`，读 `Retry-After` 头（int 化失败→None），
    `estimated_reset = _estimate_reset(retry_after)`
  - 其他状态 → `error`（`error="HTTP xxx"`）
  - `httpx.TimeoutException` → `error(timeout)`；其余异常 → `error(str(e))`
- 每个非 200 响应经 `_parse_error_type(resp)` 提取
  `{error:{type}}` 体中的 `type` 字符串（JSON 解析与字段访问全程防御，
  HTML/非 JSON 一律返回 None）。该值随 `ProbeResult.error_type` 上抛，
  是诊断分层的关键输入。

### 2.3 重置点估算

`seconds_to_utc_midnight()`（模块级函数，类方法 `_estimate_reset` 与诊断共用）：
无 `Retry-After` 时返回"距下一个 UTC 午夜的秒数"（≥1，≤86400），与网关
`FreeUsageLimitError` 携带的 retry-after 语义一致。

## 3. 账号池（pool.py）

### 3.1 auth 解析链

`AccountPool.read_auth(account)` 按 `auth_json` > `env_var` > `auth_path` 取 dict；
任何解析失败返回 None。`resolve_token()` = read_auth + `extract_access_token`。

`extract_access_token` 识别三种真实形态（键序 `access_token` → `access`）：

1. 顶层：`{"access_token": "..."}` 或 `{"type":"oauth","access":"..."}`
2. 一层嵌套：`{"opencode": {"access_token": ...}}`
3. provider 键控：`{"https://opencode.ai/zen": {"type":"oauth","access":...}}`

### 3.2 三种策略

- `round_robin`：内部游标 `_current_index` 自增取模。
- `least_used`：`min(accounts, key=health.total_count)`。
- `health`：`max(accounts, key=calculate_score(score_weights))`。

### 3.3 健康度（滑动窗口）

`AccountHealth`：

- `results: deque[bool]`，maxlen 由 `AccountPoolConfig.health_window`（默认 100）
  决定；`__post_init__` 在 maxlen 与 window 不一致时重建 deque。
- `success_rate`：**窗口非空**取 `sum(results)/len(results)`；窗口空回退
  `success_count/max(total,1)`（兼容直接构造计数的测试/场景）。
- `calculate_score(weights)` = success_rate×w1 + latency_score×w2 +
  recency_score×w3；latency_score = max(0, 1-(avg_latency-100)/900)；
  recency_score = min(1, hours_since_last_error/24)（无错误按 24h 即 1.0）。
  权重来自 `[account_pool].score_weights`，校验三键齐全、∈[0,1]、和为 1（±0.001）。
- `mark_result()`：total±、窗口 append、失败时 `consecutive_failures+=1` 并刷新
  `last_error_time`；成功清零连击、EMA 更新延迟（新值权重 0.2）。

## 4. 守护进程（daemon.py）

### 4.1 启动序列

`run()`：`_load_probe_usage()`（从状态文件恢复 `_probe_day/_probe_count`，容忍
缺字段/坏类型）→ `_acquire_lock()` → `_install_signal_handlers()` → 循环 →
`finally` 还原信号、写状态、释放锁。

### 4.2 `_probe_cycle` 的九步流水

实现按固定顺序执行（顺序本身是语义）：

1. **冷却过滤**：`respect_cooldown` 时剔除 `_cooldowns` 未到期的模型（monotonic
   时钟），命中打 INFO（含剩余秒数）。
2. **预算**：UTC 日串（`%Y%m%d`）变更即清零计数；`daily_probe_budget` 用尽→
   WARNING + `_persist_state()` + 直接 return（周期计数仍 +1）；未用尽但剩余
   不足以覆盖全部 active 模型时**裁剪列表**。
3. **账号选择**：对每个 active 模型 `pool.get_next()`；`resolve_token` 成功则
   `headers_by_model[model] = injector.build_headers(token=...)`。
4. `probe_all(active, headers, headers_by_model or None)`；`_probe_count += len(results)`。
5. 结果回写：`model_results` 更新 + 日志；非 rate_limited 结果 `mark_result`。
6. 429 处理：逐个 `_handle_rate_limited(result, account_name)`（只做失败标记 +
   轮换日志）；随后**每轮至多一次** `asyncio.to_thread(self.cleanup.full_cleanup)`。
7. 冷却布防：对 rate_limited 模型 `cooldown = monotonic + (retry_after or
   estimated_reset)`；探测正常（含 available/error）的模型解除冷却。
8. 退避：全 error 连击 `_error_streak+=1` 否则清零；`_backoff_multiplier()` =
   `1 << min(streak, 3)`（上限 8×）。
9. 快照与持久化：`pool_health`（用配置权重算 score）、`history.append({ts, models})`
   （deque maxlen=`history_size`，`_rebuild` 时按配置重设）、`next_probe`、
   `_persist_state()`。

### 4.3 `_wait` 的可中断睡眠

`asyncio.wait({stop_task, probe_task}, timeout=interval*倍数,
return_when=FIRST_COMPLETED)`；`finally` 中取消未完成任务并 gather 回收。
`stop_task` 完成即置 `_running=False`；`probe_task` 完成则清事件（SIGUSR1
"立即探测"）。

### 4.4 信号

优先 `loop.add_signal_handler`；Windows `NotImplementedError` 时回退
`signal.signal(sig, _make_signal_bridge(loop, cb))`——桥接函数用
`loop.call_soon_threadsafe` 把信号送回事件循环。退出时还原全部旧 handler。
Windows 下只有 SIGINT/SIGTERM 可用，其余信号 `getattr(signal, name, None)` 为
None 自然跳过。

### 4.5 单实例锁

- 获取：`os.open(lock, O_CREAT|O_EXCL|O_WRONLY)`；`FileExistsError` 时读 pid，
  `_pid_alive(pid)` 为真 → `DaemonLockError`（exit 1）；否则视为 stale，unlink 后
  重建。锁内容 = 自己的 pid。
- `_pid_alive`：win32 走 `ctypes` 的 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`
  + `GetExitCodeProcess`（**绝不用 `os.kill(pid,0)`——Windows 上那会终止目标进程**）；
  POSIX 用 `kill(pid,0)`，`EPERM` 视为存活。
- 释放：读回 pid 等于自己才 unlink（防止误删接管者的锁）。
- SIGHUP 重载：`_reload_config()` 重建组件后，把旧池中**同名账号**的
  `AccountHealth` 对象搬进新池（健康度/窗口跨重载保留）；`_rebuild()` 同时按新配置
  重设 history maxlen。

### 4.6 状态持久化

`_persist_state()` = `status.to_dict()` + `probe_usage{day,count}` +
`cooldowns{model: 剩余秒}` + `pid` + `updated_at` → 写 `.tmp` → `os.replace`。
写失败仅 debug 日志（诊断信息缺失不应影响探测主循环）。

## 5. 清理器（cleanup.py）

仅两类真实操作：`backup_auth_files()`（`shutil.copy` 到 `.json.bak`，内容不动）与
`purge_cache()`（rmtree 后重建空目录）。`full_cleanup(dry_run, include_cache)` 组合
二者。所有操作支持 dry_run（只记 `details`/`cleared_count`）。
曾经的 `reset_rate_limit_state`/`rotate_auth_tokens` 已移除——目标文件与字段在
opencode 中不存在（见 architecture.md §1，C1）。

## 6. 诊断（diagnostics.py）

### 6.1 出口 IP 的获取与防误判

`fetch_public_ip()` 依次请求 `api.ipify.org` → `icanhazip.com` → `ifconfig.me/ip`；
响应文本必须通过 `ipaddress.ip_address()` 校验（防 HTML 错误页/CDN 拦截页被当成
IP），否则尝试下一个；全败返回 None。请求走默认 httpx 环境——**有意**让它与探测
走同一条代理路径，这样报告里的出口 IP 就是"探测实际用的出口"。
`fetch_ip_meta()` 从 ipinfo.io 取 org/country，任何异常返回 `{}`。

`ipv6_prefix()` 对 IPv6 计算 `/64` 网络（网关的聚合粒度），IPv4/非法输入返回 None。

### 6.2 auth 盘点（无泄漏）

`inspect_auth_files()` 输出每个候选的 `exists/shape/has_token`。`_auth_shape` 分类：
`single-entry:{type}`（顶层 oauth/api/wellknown）、`provider-keyed:{types}`、
`empty`、`unknown`、`unreadable ({异常类型})`。**只输出类型布尔，不输出任何值**——
有测试断言 token 明文不出现在报告与 JSON 中。

### 6.3 findings 生成与判定矩阵

`_probe_findings(result, model)` 按探测结果产出 `Finding{severity,title,detail,
remedy}` 列表并决定 `verdict/exit_code`：

| 探测结果 | 判定 | 关键 findings |
|----------|------|---------------|
| available | ok / 0 | 延迟与配额健康提示 |
| 429 + `FreeUsageLimitError` | rate_limited / 1 | 配额用尽（IP 键）→ 重置时刻（`seconds_to_utc_midnight` + 本地时区换算）→ "换账号无效" → 出口 IP/节点切换核验清单（CLI 不读系统代理、共享节点、/64） |
| 429 + `RateLimitError` 等 | rate_limited / 1 | "该限制与出口 IP 无关"，等待窗口 |
| error + `error_type` 非空 | error / 2 | **已到达网关**：按映射表解释（`server_error` → 上游错误，与配额无关） |
| error 无 `error_type` | error / 2 | 未达网关：网络/代理链路排查（代理变量在→查端口与节点；不在→提醒 CLI 不读系统代理） |

`run_diagnostics()` 的每个外部调用都在 executor 中执行并全程 try/except；
探测本身若抛异常（不应发生）被兜底为内部 error finding。

## 7. CLI 与补全（parser.py / completions.py / cli.py）

- **共享旗标技巧**：`--json`/`--dry-run` 通过公共父 parser 注入子命令，默认值用
  `argparse.SUPPRESS`——子命令未显式给出时不覆盖父层值，实现"写在前后都生效"。
- **banner 抑制**：`should_print_banner()` 对 `_STRUCTURED_COMMANDS`
  （check/diagnose/probe/headers/generate-*/completions）与一切 `--json` 输出隐藏
  启动横幅，保证管道纯净。
- **补全派生**：`_completion_payload()` 反射真实 parser——子命令取
  `help`/`description`，选项取 `option_strings`（排除全局旗标），`--strategy` 取
  choices，`probe` 位置参数取 `FREE_MODELS + ["all"]`。三个 shell 模板用 `%`-格式
  （`pyproject` 中 `UP031` 豁免有注释说明：大括号密集的 shell 模板用 f-string 会
  双花括号地狱）。**新增子命令只需注册 parser**，补全/测试清单同步即自动覆盖。
- **退出码约定**：0 成功；1 业务失败（清理出错/探测被限/rotate 无账号/daemon 锁冲突/
  diagnose 被限流）；2 配置错误/argparse 错误/diagnose 网络层失败；130 Ctrl+C。

## 8. 日志（logs.py）

- `JSONFormatter`：`timestamp` 从 `record.created` 用 UTC 格式化（毫秒精度，
  `Z` 后缀）——修正过"本地时间标 Z"的缺陷；extra 字段透传，stdlib 内部键
  （含 3.12 新增的 `taskName`）由 `_STDLIB_LOG_KEYS` 冻结集过滤。
- `setup_logging`：win32 先对 stdout/stderr `reconfigure(errors="replace")`
  （GBK 控制台防 UnicodeEncodeError），handler 只挂 root 一份，httpx/httpcore
  压到 WARNING。

## 9. 测试实现要点

- **动态加载**：`test_build_binary.py` 用 importlib 从文件路径加载构建脚本，避免
  PyInstaller 真实执行；并断言脚本使用包入口 shim（防单文件时代的回归）。
- **网络 mock**：`pytest-httpx` 的注册响应是**单次消费**——单请求场景用
  `add_response`，多轮探测场景一律 fake probe（曾因版本行为差异导致基线 error）。
- **诊断隔离**：`_patch_env` 助手统一替换 ModelProber/get_opencode_version/
  get_opencode_auth_files/fetch_public_ip/fetch_ip_meta 并清空代理环境变量，
  保证判定矩阵测试与真实网络完全无关。
- **重置点测试**：`_estimate_reset(None)` 断言"等于重算的距午夜秒数"而非固定值，
  任意时刻运行都稳定。

## 10. 已知实现限制

1. `cooldowns` 以绝对 UTC 时刻随状态文件持久化，daemon 重启后恢复剩余冷却（R4；预算计数同样跨重启）。
2. `_merge` 对嵌套表做一层深合并；部分 `score_weights` 与"和为 1"校验冲突时会被明确拒绝（见 config 章节）。
3. 出口 IP 检测反映的是**本工具**的路径；opencode CLI 是否同路径取决于其启动环境
   （诊断报告会明示这一点）。
4. 补全的 fish 分支按"选项是否含 choices"过滤，极少数组合可能少列选项。
5. Windows 任务计划 XML 以 UTF-8 声明 + UTF-8 文本输出（R4 已对齐，可直接导入 schtasks）。
