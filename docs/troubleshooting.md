> **注：本文档为早期拆分文档，已停止维护。** 部分内容属于当时的设计规划，
> 与当前实现存在出入（差异清单见 MANUAL.md 第 12 节）。
> **权威文档请以 [MANUAL.md](../MANUAL.md) 为准。**

# 故障排查指南

## 快速诊断决策树

```
遇到问题
    │
    ├─► 运行 `opencode-rate-limiter check --json` 查看整体状态
    │       │
    │       ├─ daemon.running = false  ──► 守护进程未运行，检查服务状态
    │       │
    │       ├─ models.*.status = "rate_limited"  ──► 该模型被限流，等待或轮换账号
    │       │
    │       ├─ models.*.status = "error"  ──► 网络/认证错误，检查下方对应章节
    │       │
    │       └─ account_pool.current = "backupN"  ──► 已轮换到备用账号
    │
    └─► 运行 `opencode-rate-limiter probe <model> --json` 验证单模型
```

---

## 错误代码对照表

| HTTP 状态 | 错误类型 | 含义 | 处理建议 |
|-----------|----------|------|----------|
| 200 | - | 正常 | 无 |
| 401 | AuthenticationError | Token 失效/过期 | `opencode login` 重新登录 |
| 403 | PermissionDenied | 账号无权限 | 检查账号状态，联系支持 |
| 429 | FreeUsageLimitError | 免费额度耗尽/限流 | 等待冷却、轮换账号、升级付费 |
| 429 | RateLimitError | 通用限流 | 指数退避重试 |
| 500 | InternalServerError | 服务端错误 | 等待恢复，重试 |
| 502/503/504 | BadGateway/ServiceUnavailable | 网关/服务不可用 | 等待恢复，重试 |
| 0/timeout | ConnectError/TimeoutException | 网络不通 | 检查网络、代理、DNS |

---

## 常见问题分类

### 1. 认证相关

#### 症状：所有模型返回 401
```
原因: access_token 过期或被撤销
排查:
  1. opencode-rate-limiter check --json → account_pool.accounts.*.healthy = false
  2. cat ~/.opencode/auth.json → 检查 access_token 字段
  3. 运行 opencode auth check 验证
解决:
  opencode login
  # 或
  opencode-rate-limiter rotate --strategy health
```

#### 症状：Token 正常但仍 401
```
原因: 请求头缺失导致被识别为匿名客户端
排查:
  1. opencode-rate-limiter headers --export | source
  2. curl -H "User-Agent: opencode/1.18.16" ... 测试
解决:
  确保使用官方头部，或升级 opencode-rate-limiter 版本
```

### 2. 限流相关

#### 症状：Silent Limit (429 无 Retry-After)
```
特征: HTTP 429 + FreeUsageLimitError + 无标准头部
原因: 免费模型匿名限流或官方客户端也受限
排查:
  1. opencode-rate-limiter probe deepseek-v4-flash-free --json
  2. 检查 estimated_reset 字段 (本地估算)
解决:
  1. 等待 estimated_reset 秒
  2. opencode-rate-limiter quick (清理本地退避状态)
  3. opencode-rate-limiter rotate (切换账号)
  4. 降低请求频率
```

#### 症状：官方 CLI 正常但脚本/代理 429
```
原因: 请求头不匹配官方 CLI
排查:
  diff <(opencode-rate-limiter headers --json) <(你的请求头)
解决:
  使用 opencode-rate-limiter headers --export 生成完整头部
```

#### 症状：本地清理后仍提示限流
```
原因: 服务端冷却未结束，本地清理仅解除本地挂起
排查:
  opencode-rate-limiter probe <model> --json → estimated_reset > 0
解决:
  等待服务端冷却结束 (通常 1-5 分钟)
  或切换账号/模型
```

### 3. 网络相关

#### 症状：连接超时/拒绝
```
排查:
  1. ping opencode.ai
  2. curl -v https://opencode.ai/zen/v1/models
  3. 检查代理设置 (HTTP_PROXY/HTTPS_PROXY)
  4. 检查防火墙/杀毒软件
解决:
  配置正确的代理环境变量
  或使用 --config 指定代理设置 (Phase 2 支持)
```

#### 症状：DNS 解析失败
```
排查:
  nslookup opencode.ai
  dig opencode.ai
解决:
  更换 DNS (1.1.1.1, 8.8.8.8)
  检查 /etc/hosts
```

### 4. 配置相关

#### 症状：配置文件不生效
```
排查:
  1. opencode-rate-limiter check --json → 查看实际生效配置
  2. 检查文件路径: ~/.config/opencode-rate-limiter/config.toml
  3. 验证 TOML 语法: toml-lint config.toml
解决:
  修正配置文件路径或语法
  使用 --config 显式指定
```

#### 症状：账号池轮换失败
```
排查:
  1. 检查 auth_path 文件是否存在
  2. 检查文件权限 (600)
  3. 验证 JSON 格式有效
解决:
  修正路径、权限、格式
```

### 5. 守护进程相关

#### 症状：systemd 服务启动失败
```
排查:
  journalctl --user -u opencode-rate-limiter -n 50
常见原因:
  - ExecStart 路径错误
  - 环境变量未设置
  - 配置文件语法错误
  - 权限不足
```

#### 症状：守护进程频繁重启
```
排查:
  journalctl --user -u opencode-rate-limiter -f
常见原因:
  - 内存泄漏 (MemoryMax 过小)
  - 未捕获异常导致崩溃
  - 探测间隔过短触发上游限流
```

---

## 日志分析指南

### 关键日志模式

```bash
# 查看限流事件
grep "rate_limited" /var/log/opencode-rate-limiter.log | jq .

# 查看账号轮换
grep "Account rotated" /var/log/opencode-rate-limiter.log | jq .

# 查看清理操作
grep "cleanup" /var/log/opencode-rate-limiter.log | jq .

# 查看错误
grep '"level": "ERROR"' /var/log/opencode-rate-limiter.log | jq .
```

### 日志字段说明

| 字段 | 含义 |
|------|------|
| `timestamp` | ISO 8601 UTC 时间 |
| `level` | DEBUG/INFO/WARNING/ERROR |
| `logger` | 模块名 (daemon/prober/cleanup/pool) |
| `message` | 人类可读消息 |
| `model` | 相关模型名 |
| `status` | available/rate_limited/error/unknown |
| `latency_ms` | 探测延迟 |
| `retry_after` | 服务端建议等待秒数 |
| `estimated_reset` | 本地估算重置秒数 |
| `trigger` | 触发原因 (429/manual/schedule) |

---

## 调试技巧

### 1. 启用调试日志

```bash
# 临时
opencode-rate-limiter -vv daemon

# 永久 (配置文件)
# logging.level = "DEBUG" (Phase 1 实现)
```

### 2. 干运行模式

```bash
# 预览将执行的操作
opencode-rate-limiter quick --dry-run
opencode-rate-limiter deep --dry-run
opencode-rate-limiter rotate --dry-run
```

### 3. 单步执行

```bash
# 仅探测不清理
opencode-rate-limiter probe all --json

# 仅清理不探测
opencode-rate-limiter quick

# 仅轮换不清理
opencode-rate-limiter rotate --strategy health
```

### 4. 手动模拟 429

```bash
# 使用测试端点或 mock 服务器
# Phase 2 提供测试工具
```

---

## 性能问题排查

| 症状 | 可能原因 | 排查方法 |
|------|----------|----------|
| 探测延迟 > 5s | 网络慢/超时设置大 | 检查 `probe_timeout_seconds` |
| 内存增长 | 日志积累/未清理 | 检查日志轮转、内存限制 |
| CPU 高 | 探测间隔太短 | 增加 `interval_seconds` |
| 磁盘满 | 缓存未清理 | 检查 `cleanup.cache_dirs` |

---

## 联系支持

如果以上方法无法解决，请收集以下信息提交 Issue：

1. `opencode-rate-limiter check --json` 输出
2. `opencode-rate-limiter --version`
3. 相关日志片段 (脱敏后)
4. 配置文件 (脱敏后)
5. 操作系统、Python 版本
6. 复现步骤