> **注：本文档为早期拆分文档，已停止维护。** 部分内容属于当时的设计规划，
> 与当前实现存在出入（差异清单见 MANUAL.md 第 12 节）。
> **权威文档请以 [MANUAL.md](../MANUAL.md) 为准。**

# 进阶用法指南

## 多账号轮换策略详解

### 策略对比

| 策略 | 算法 | 优点 | 缺点 | 适用场景 |
|------|------|------|------|----------|
| `round_robin` | 顺序轮询 | 简单、公平 | 不考虑健康度 | 账号等价、测试环境 |
| `least_used` | 使用计数最少优先 | 负载均衡 | 不考虑成功率 | 长期运行、账号权重相同 |
| `health` | 综合评分 (成功率 50% + 延迟 30% + 近期错误 20%) | 智能、自适应 | 计算开销稍大 | **生产环境推荐** |

### 健康度评分算法

```python
def calculate_health_score(account: Account) -> float:
    """
    评分范围: 0.0 - 1.0 (越高越健康)

    因子权重:
    - 成功率 (50%): 近 100 次请求成功比例
    - 平均延迟 (30%): 归一化到 0-1，延迟越低分越高
    - 最近错误时间 (20%): 距离上次错误越久分越高
    """
    success_rate = account.success_count / max(account.total_count, 1)  # 50%

    # 延迟归一化: 假设 100ms 基准，>1000ms 视为 0 分
    latency_score = max(0, 1 - (account.avg_latency_ms - 100) / 900)  # 30%

    # 错误时间衰减: 1小时内有错误扣分，24小时后恢复
    hours_since_error = (now - account.last_error_time).total_seconds() / 3600
    error_score = min(1.0, hours_since_error / 24)  # 20%

    return success_rate * 0.5 + latency_score * 0.3 + error_score * 0.2
```

### 自定义策略 (Phase 4+)

```toml
[account_pool]
strategy = "custom"
custom_strategy = "my_module.my_strategy_class"
```

---

## CI/CD 集成

### GitHub Actions 示例

```yaml
# .github/workflows/opencode-rate-limiter.yml
name: OpenCode Rate Limiter Check

on:
  schedule:
    - cron: '*/15 * * * *'  # 每 15 分钟
  workflow_dispatch:

jobs:
  check-rate-limit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Setup opencode-rate-limiter
        uses: astral-sh/setup-uv@v4
        with:
          version: "latest"
      
      - name: Install
        run: uvx --from git+https://github.com/... opencode-rate-limiter
      
      - name: Check models
        run: |
          opencode-rate-limiter probe all --json > probe-result.json
          cat probe-result.json
      
      - name: Alert on rate limited
        if: failure()
        run: |
          # 发送 Slack/Discord/Email 通知
          curl -X POST $SLACK_WEBHOOK -d '{"text": "OpenCode free models rate limited!"}'
      
      - name: Auto cleanup if needed
        run: |
          # 如果有模型被限流，自动清理
          jq -e '.[] | select(.status == "rate_limited")' probe-result.json && \
          opencode-rate-limiter quick || true
```

### GitLab CI 示例

```yaml
# .gitlab-ci.yml
opencode_rate_limit_check:
  stage: monitor
  image: python:3.11-slim
  before_script:
    - pip install uv
    - uvx --from git+https://github.com/... opencode-rate-limiter
  script:
    - opencode-rate-limiter check --json > health.json
    - |
      if jq -e '.models | to_entries[] | select(.value.status == "rate_limited")' health.json; then
        echo "Rate limit detected, triggering cleanup"
        opencode-rate-limiter quick
      fi
  only:
    - schedules
  tags:
    - docker
```

### 预提交钩子

```yaml
# .pre-commit-config.yaml (项目内)
- repo: local
  hooks:
    - id: opencode-rate-limit-check
      name: Check OpenCode rate limits before commit
      entry: opencode-rate-limiter probe deepseek-v4-flash-free --json
      language: system
      pass_filenames: false
      always_run: true
      verbose: true
```

---

## 自定义探测模型

### 添加新免费模型

```toml
[daemon]
models = [
  "deepseek-v4-flash-free",
  "nemotron-3-ultra-free",
  "big-pickle",
  "mimo-v2.5-free",
  "custom-model-free"  # 新增
]
```

### 使用非标准端点

```toml
# Phase 2+ 支持自定义端点
[prober]
endpoints = {
  "custom-model-free" = "https://custom.example.com/v1/chat/completions"
}
custom_headers = {
  "custom-model-free" = { "Authorization" = "Bearer ${CUSTOM_API_KEY}" }
}
```

### 探测载荷自定义

```toml
[prober]
probe_payloads = {
  "deepseek-v4-flash-free" = { max_tokens = 1, temperature = 0 },
  "nemotron-3-ultra-free" = { max_tokens = 5, temperature = 0.1 }
}
default_payload = { max_tokens = 1, temperature = 0 }
```

---

## 头部注入器高级用法

### 导出环境变量供 curl/HTTPie 使用

```bash
# Bash/Zsh
eval "$(opencode-rate-limiter headers --export)"

# 现在可以直接使用
curl -H "$USER_AGENT" -H "$X_OPENCODE_CLIENT" -H "$X_OPENCODE_VERSION" \
  -H "Authorization: Bearer $OPENCODE_API_KEY" \
  https://opencode.ai/zen/v1/chat/completions

# Fish
source (opencode-rate-limiter headers --export | psub)
```

### 生成完整 curl 命令

```bash
opencode-rate-limiter headers --model deepseek-v4-flash-free --json | \
jq -r '
  "curl -X POST https://opencode.ai/zen/v1/chat/completions \
  " + (to_entries | map("-H \"\(.key): \(.value)\"") | join(" ")) + "
  -d '{\"model\":\"deepseek-v4-flash-free\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}],\"max_tokens\":100}'"
'
```

### 集成到代理/网关

```nginx
# Nginx 配置示例
location /zen/v1/ {
    proxy_pass https://opencode.ai/zen/v1/;
    
    # 注入官方头部
    proxy_set_header User-Agent "opencode/1.18.16";
    proxy_set_header x-opencode-client "opencode-cli";
    proxy_set_header x-opencode-version "1.18.16";
    
    # 透传认证
    proxy_set_header Authorization $http_authorization;
}
```

---

## 缓存管理策略

### 分级清理策略

```toml
[cleanup]
# 级别 1: 仅限流锁 (快速、安全)
level1_files = ["*rate_limit*.json", "*backoff*.json"]

# 级别 2: 状态文件 (中等)
level2_files = ["state.json", "auth.json.rate_limited_until"]

# 级别 3: 完整缓存 (彻底、较慢)
level3_dirs = ["cache/", "logs/", "analytics/"]

# 命令映射
# quick  → level1 + level2
# deep   → level1 + level2 + level3
```

### 定时清理配置

```toml
[cleanup.scheduler]
# 每日清理
daily_at = "03:00"
# 每周深度清理
weekly_on = "sunday"
weekly_at = "04:00"
# 保留最近 N 份备份
backup_retention = 7
```

---

## 监控告警集成

### Prometheus 规则示例

```yaml
# prometheus/rules/opencode-rate-limiter.yml
groups:
  - name: opencode-rate-limiter
    rules:
      - alert: OpenCodeModelRateLimited
        expr: opencode_model_status{status="rate_limited"} == 1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "OpenCode model {{ $labels.model }} is rate limited"
          description: "Model has been rate limited for 5 minutes. Estimated reset: {{ $value }}s"
      
      - alert: OpenCodeAccountPoolExhausted
        expr: opencode_account_pool_healthy_accounts == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "No healthy accounts in pool"
          description: "All accounts are rate limited or unhealthy"
      
      - alert: OpenCodeDaemonDown
        expr: up{job="opencode-rate-limiter"} == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "OpenCode rate limiter daemon is down"
```

### Grafana 仪表板关键指标

- `opencode_model_status` - 各模型状态 (available/rate_limited/error)
- `opencode_probe_latency_seconds` - 探测延迟分布
- `opencode_cleanup_total` - 清理操作计数
- `opencode_account_pool_current` - 当前活跃账号
- `opencode_account_health_score` - 账号健康度趋势

---

## 与其他工具协作

### 配合 opencode CLI

```bash
# 登录前先检查
opencode-rate-limiter check --json

# 登录后验证
opencode login
opencode-rate-limiter probe deepseek-v4-flash-free --json
```

### 配合 VS Code / Cursor

```json
// settings.json
{
  "opencode.rateLimiter.checkOnStartup": true,
  "opencode.rateLimiter.autoCleanup": true,
  "opencode.rateLimiter.daemon.enabled": true
}
```

### 配合 oh-my-opencode / 自定义代理

```yaml
# oh-my-opencode config
model_fallback: true
agents:
  coder:
    model: opencode/deepseek-v4-flash-free
    fallback_models:
      - opencode/nemotron-3-ultra-free
      - opencode/big-pickle
```

---

## 性能优化建议

### 1. 减少探测开销

```toml
[daemon]
interval_seconds = 120  # 从 30 增加到 120 秒
models = ["deepseek-v4-flash-free"]  # 仅探测主力模型
probe_timeout_seconds = 5.0  # 缩短超时
```

### 2. 复用 HTTP 连接

```toml
[prober]
http2 = true
connection_pool_size = 5
keepalive_timeout = 30
```

### 3. 批量操作优化

```bash
# 并行清理多个账号
opencode-rate-limiter rotate --strategy health --parallel

# 批量探测 (默认并发)
opencode-rate-limiter probe all --concurrent 5
```

---

## 安全加固

### 1. Token 加密存储

```toml
[account_pool]
encryption = "age"  # 或 "sops", "gpg"
encrypted_accounts = [
  { name = "primary", encrypted_path = "~/.config/opencode-rate-limiter/accounts/primary.age" }
]
```

### 2. 最小权限文件权限

```bash
# 自动设置
chmod 600 ~/.opencode/auth.json
chmod 600 ~/.config/opencode-rate-limiter/config.toml
chmod 700 ~/.config/opencode-rate-limiter/
```

### 3. 审计日志

```toml
[audit]
enabled = true
log_file = "~/.local/share/opencode-rate-limiter/audit.log"
log_format = "json"
events = ["cleanup", "rotate", "probe_failure", "config_change"]
```

---

## 扩展开发

### 插件接口 (规划中)

```python
# plugins/my_custom_cleaner.py
from opencode_rate_limiter.plugins import CleanerPlugin


class MyCustomCleaner(CleanerPlugin):
    name = "my_custom_cleaner"

    def clean(self, config: CleanupConfig) -> CleanupResult:
        # 自定义清理逻辑
        pass

    def should_run(self, trigger: str) -> bool:
        return trigger in ("429", "schedule")
```

### 注册插件

```toml
[plugins]
cleaners = ["my_custom_cleaner"]
probers = []
notifiers = ["slack", "email"]
```

---

## 常见问题 FAQ

### Q: 为什么不直接用 `opencode login` 解决限流？
A: `login` 只刷新 Token，不清理本地退避状态 (`state.json`、`rate_limited_until`)，服务端冷却未结束时仍会被本地拦截。

### Q: 守护进程会消耗大量配额吗？
A: 每次探测仅消耗 1 个输出 token，每模型每分钟约 1/interval 请求。默认 30 秒间隔 = 2 RPM/模型，远低于 15-20 RPM 限额。

### Q: 可以同时运行多个守护进程吗？
A: 不建议。使用文件锁防止并发，第二个实例会检测到锁并退出。

### Q: Windows 上如何自动启动？
A: 使用 Task Scheduler (见 daemon-mode.md) 或放入启动文件夹。

### Q: 如何手动指定 opencode 版本用于头部生成？
A: `OPENCODE_VERSION=1.18.16 opencode-rate-limiter headers --export`

### Q: 支持代理吗？
A: 支持标准环境变量 `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` (Phase 2 完善)。

---

## 版本兼容性矩阵

| opencode-rate-limiter | OpenCode CLI | Python | 备注 |
|----------------------|--------------|--------|------|
| 0.1.x | 1.18+ | 3.11+ | 初始版本 |
| 0.2.x | 1.19+ | 3.11+ | 头部模板更新 |
| 1.0.x | 1.20+ | 3.12+ | 稳定 API |

> 建议：保持 opencode-rate-limiter 与 OpenCode CLI 同步更新