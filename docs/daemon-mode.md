> **注：本文档为早期拆分文档，已停止维护。** 部分内容属于当时的设计规划，
> 与当前实现存在出入（差异清单见 MANUAL.md 第 12 节）。
> **权威文档请以 [MANUAL.md](../MANUAL.md) 为准。**

# 守护进程模式部署指南

## 概述

`opencode-rate-limiter daemon` 以后台进程运行，周期性探测免费模型可用性，遇到限流自动触发清理和账号轮换。

## 启动方式

### 前台运行 (调试用)

```bash
opencode-rate-limiter daemon --interval 30 --models "deepseek-v4-flash-free,nemotron-3-ultra-free"
```

### 后台运行 (生产推荐)

#### Linux/macOS: systemd (用户级)

```bash
# 1. 生成服务文件
opencode-rate-limiter generate-systemd > ~/.config/systemd/user/opencode-rate-limiter.service

# 2. 启用并启动
systemctl --user daemon-reload
systemctl --user enable --now opencode-rate-limiter

# 3. 查看状态
systemctl --user status opencode-rate-limiter
systemctl --user logs -f opencode-rate-limiter
```

生成的服务文件内容：
```ini
[Unit]
Description=OpenCode Rate Limiter Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
ExecStart=%h/.local/bin/opencode-rate-limiter daemon
Restart=on-failure
RestartSec=10
Environment=OPENCODE_RATE_LIMITER_CONFIG=%h/.config/opencode-rate-limiter/config.toml
# 可选: 限制资源
MemoryMax=100M
CPUQuota=10%

[Install]
WantedBy=default.target
```

#### macOS: launchd

```bash
# 1. 生成 plist
opencode-rate-limiter generate-launchd > ~/Library/LaunchAgents/com.opencode.ratelimiter.plist

# 2. 加载
launchctl load ~/Library/LaunchAgents/com.opencode.ratelimiter.plist
launchctl start com.opencode.ratelimiter
```

生成的 plist：
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.opencode.ratelimiter</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/opencode-rate-limiter</string>
        <string>daemon</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/opencode-rate-limiter.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/opencode-rate-limiter.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>OPENCODE_RATE_LIMITER_CONFIG</key>
        <string>/Users/USERNAME/.config/opencode-rate-limiter/config.toml</string>
    </dict>
</dict>
</plist>
```

#### Windows: Task Scheduler

```powershell
# 1. 生成任务 XML
opencode-rate-limiter generate-task > %TEMP%\opencode-rate-limiter.xml

# 2. 导入任务
schtasks /create /xml %TEMP%\opencode-rate-limiter.xml /tn "OpenCode Rate Limiter"

# 3. 启动任务
schtasks /run /tn "OpenCode Rate Limiter"
```

生成的任务 XML：
```xml
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>OpenCode Rate Limiter Daemon</Description>
    <Author>opencode-rate-limiter</Author>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
  </Settings>
  <Actions>
    <Exec>
      <Command>opencode-rate-limiter.exe</Command>
      <Arguments>daemon</Arguments>
      <WorkingDirectory>%USERPROFILE%</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
```

#### Docker 容器

```dockerfile
# Dockerfile
FROM python:3.11-slim
RUN pip install opencode-rate-limiter
ENTRYPOINT ["opencode-rate-limiter", "daemon"]
```

```yaml
# docker-compose.yml
services:
  opencode-rate-limiter:
    build: .
    environment:
      - OPENCODE_RATE_LIMITER_CONFIG=/config/config.toml
    volumes:
      - ./config:/config:ro
      - ~/.opencode:/root/.opencode:ro
    restart: unless-stopped
```

---

## 信号处理

| 信号 | 行为 |
|------|------|
| `SIGTERM` (15) | 优雅关闭：停止探测循环，等待当前任务完成，清理资源 |
| `SIGINT` (2) | 同上 (Ctrl+C) |
| `SIGHUP` (1) | 重新加载配置文件 (不重启进程) |
| `SIGUSR1` (10) | 立即执行一次探测 (手动触发) |
| `SIGUSR2` (12) | 输出当前状态到日志 (健康度、账号池状态) |

Windows 对应：
- `CTRL_C_EVENT` → SIGINT
- `CTRL_BREAK_EVENT` → SIGTERM
- 服务停止 → SIGTERM

> **注意**：`SIGHUP`/`SIGUSR1`/`SIGUSR2` 为 Unix 专属信号，Windows 下不存在（守护进程将自动跳过）。Windows 上优雅停止依赖 `CTRL_C_EVENT`/`CTRL_BREAK_EVENT`（由 `signal.signal` 兜底安装）。所有信号处理在退出后自动还原。

---

## 监控与观测

### 日志输出

守护进程输出结构化 JSON 日志：

```json
{"timestamp": "2026-09-10T12:00:00Z", "level": "INFO", "logger": "daemon", "message": "Starting probe cycle", "models": 3}
{"timestamp": "2026-09-10T12:00:01Z", "level": "INFO", "logger": "prober", "message": "Probe completed", "model": "deepseek-v4-flash-free", "status": "available", "latency_ms": 45}
{"timestamp": "2026-09-10T12:00:02Z", "level": "WARNING", "logger": "prober", "message": "Rate limited detected", "model": "nemotron-3-ultra-free", "status": "rate_limited", "retry_after": null}
{"timestamp": "2026-09-10T12:00:03Z", "level": "INFO", "logger": "cleanup", "message": "Auto cleanup triggered", "trigger": "429", "model": "nemotron-3-ultra-free"}
{"timestamp": "2026-09-10T12:00:04Z", "level": "INFO", "logger": "pool", "message": "Account rotated", "from": "primary", "to": "backup1", "strategy": "health"}
```

### 状态文件

守护进程在每次探测周期结束和退出时，将运行状态原子写入状态文件（Windows: `%LOCALAPPDATA%\opencode-rate-limiter\daemon.json`；Linux: `~/.local/state/opencode-rate-limiter/daemon.json`）。`opencode-rate-limiter check --json` 会合并输出该状态（`running`、`uptime_seconds`、`last_probe`、`next_probe`、各模型最新探测结果、清理计数）。状态文件不可写时不报错（仅 debug 日志）。

### 健康检查端点

```bash
# 手动触发健康检查
opencode-rate-limiter check --json

# 输出示例
{
  "timestamp": "2026-09-10T12:00:00Z",
  "daemon": {
    "running": true,
    "uptime_seconds": 3600,
    "last_probe": "2026-09-10T11:59:00Z",
    "next_probe": "2026-09-10T12:00:00Z"
  },
  "models": {
    "deepseek-v4-flash-free": {"status": "available", "last_check": "...", "latency_ms": 45},
    "nemotron-3-ultra-free": {"status": "rate_limited", "last_check": "...", "estimated_reset": 300},
    "big-pickle": {"status": "available", "last_check": "...", "latency_ms": 52}
  },
  "account_pool": {
    "current": "primary",
    "accounts": {
      "primary": {"healthy": true, "success_rate": 0.98, "avg_latency_ms": 48},
      "backup1": {"healthy": true, "success_rate": 0.95, "avg_latency_ms": 55}
    }
  },
  "cleanup": {
    "last_cleanup": "2026-09-10T11:55:00Z",
    "total_cleanups": 12
  }
}
```

### Prometheus 指标 (可选)

如需 Prometheus 指标，可配合 `prometheus-client` 库在单独端口暴露 `/metrics`。

---

## 故障排查

### 守护进程无法启动

```bash
# 检查配置语法
opencode-rate-limiter check --json

# 检查二进制可执行
opencode-rate-limiter --version

# 检查权限
ls -la ~/.local/bin/opencode-rate-limiter
```

### 探测不工作

```bash
# 手动探测测试
opencode-rate-limiter probe deepseek-v4-flash-free --json

# 检查网络连通性
curl -v https://opencode.ai/zen/v1/chat/completions
```

### 账号轮换不生效

```bash
# 检查账号池配置
opencode-rate-limiter check --json | jq .account_pool

# 手动轮换测试
opencode-rate-limiter rotate --strategy health
```

### systemd 服务失败

```bash
# 查看详细日志
journalctl --user -u opencode-rate-limiter -f

# 检查服务文件
systemctl --user cat opencode-rate-limiter
```

---

## 性能调优

| 参数 | 推荐值 | 说明 |
|------|--------|------|
| `interval_seconds` | 60-300 | 太短增加上游压力，太长延迟检测 |
| `probe_timeout_seconds` | 10-30 | 根据网络环境调整 |
| `models` 数量 | 3-5 | 并发探测，避免过多并发 |
| `auto_cleanup_on_429` | true | 生产环境建议开启 |

---

## 安全考虑

1. **最小权限**：守护进程仅需读写 `~/.opencode/` 和配置目录
2. **敏感信息**：auth.json 包含 Token，确保文件权限 `600`
3. **网络隔离**：仅需访问 `opencode.ai:443`
4. **资源限制**：建议配置 systemd `MemoryMax` 和 `CPUQuota`