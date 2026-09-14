# opencode-rate-limiter

OpenCode 免费模型限流缓解与账号池管理工具

## 状态

**v0.4.0** - Phase 1–7 全部完成 + 正确性/质量大修（发布流程见 [docs/release.md](docs/release.md)，迭代计划见 [docs/roadmap.md](docs/roadmap.md)）

> 文档：**[使用手册](docs/user-guide.md)** · [功能介绍](docs/features.md) ·
> [技术架构](docs/architecture.md) · [实现细节](docs/implementation.md) ·
> [路线图](docs/roadmap.md) · [发布流程](docs/release.md) ·
> **[MANUAL.md](MANUAL.md)**（命令/配置权威参考）。

## 快速开始

```bash
# 安装开发依赖
uv sync --dev

# 运行测试
uv run pytest

# 代码检查
uv run ruff check .
uv run mypy .

# 临时运行 (开发模式)
uv run opencode-rate-limiter --help
```

## 核心命令

| 命令 | 说明 |
|------|------|
| `quick` | 快速维护：备份 auth.json（服务端限额无法本地解除） |
| `deep` | 深度维护：quick + 清缓存 |
| `probe` | 探测免费模型可用性 |
| `headers` | 输出官方 CLI 兼容请求头 |
| `rotate` | 手动轮换账号池 |
| `check` | 健康检查聚合输出（含守护进程状态） |
| `diagnose` | Zen 限额诊断（出口 IP / 代理 / 429 分层 / 建议） |
| `daemon` | 后台守护进程模式（单实例锁） |
| `generate-systemd` | 生成 systemd 服务文件 |
| `generate-launchd` | 生成 macOS launchd plist |
| `generate-task` | 生成 Windows 任务计划 XML |
| `generate-config` | 生成默认配置文件（已存在需 `--force`） |
| `completions` | 生成 shell 补全脚本 (bash/zsh/fish/powershell) |

## Shell 补全

```bash
# bash
opencode-rate-limiter completions bash > \
    ~/.local/share/bash-completion/completions/opencode-rate-limiter.bash

# zsh
opencode-rate-limiter completions zsh > \
    ~/.zsh/functions/_opencode-rate-limiter

# fish
opencode-rate-limiter completions fish > \
    ~/.config/fish/completions/opencode-rate-limiter.fish
```

补全脚本由真实 CLI parser 派生（子命令、参数、模型列表单一数据源），可用
`scripts/generate_completions.py` 一键写入 `completion/` 目录。

结构化输出命令（`check`、`probe`、`headers`、`generate-*`、`completions`）不打印
启动横幅，保证 `check --json | jq` 等管道场景输出纯净。

## 二进制打包

```bash
pip install ".[build]"        # pyinstaller
python scripts/build_binary.py
```

产物输出到 `dist/`，命名含平台标签（如 `opencode-rate-limiter-windows-amd64.exe`），
并在构建完成后自动以 `--help` 冒烟验证。

- 单文件（`--onefile`），随包打入 httpx / platformdirs / tomli_w
- `--strip` 仅 Unix 启用（Windows 链接器无意义）
- macOS 附带 `com.opencode.ratelimiter` bundle identifier
- 打包脚本平台细节见 `tests/test_build_binary.py`

## 配置文件

配置文件位置：`~/.config/opencode-rate-limiter/config.toml`

完整字段表见 [MANUAL.md](MANUAL.md) §5；配置场景速查见 [docs/user-guide.md](docs/user-guide.md)

## 开发路线图

- [x] Phase 0: 脚手架、pyproject、CI、pre-commit
- [x] Phase 1: 配置管理、路径解析、日志系统
- [x] Phase 2: 头部注入器、模型探测、账号池、清理器
- [x] Phase 3: 守护进程模式
- [x] Phase 4: CLI 界面整合
- [x] Phase 5: 文档与补全生成
- [x] Phase 6: 测试与二进制打包
- [x] Phase 7: 发布（tag 驱动流水线，见 `docs/release.md`）

## 许可证

MIT License