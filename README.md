# opencode-rate-limiter

OpenCode 免费模型限流缓解与账号池管理工具

## 状态

**Phase 1-6 完成** - 剩余 Phase 7（发布）

> 完整技术说明与使用手册见 **[MANUAL.md](MANUAL.md)**（文档与实测代码逐条核对，
> `docs/` 下早期文档的出入以 MANUAL.md 为准）。

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
| `quick` | 快速解除限流（清理退避锁 + 重置 Token） |
| `deep` | 深度清理（+ 清除缓存 + 强制重新登录） |
| `probe` | 探测免费模型可用性 |
| `headers` | 输出官方 CLI 兼容请求头 |
| `rotate` | 手动轮换账号池 |
| `check` | 健康检查聚合输出（含守护进程状态） |
| `daemon` | 后台守护进程模式（单实例锁） |
| `generate-systemd` | 生成 systemd 服务文件 |
| `generate-launchd` | 生成 macOS launchd plist |
| `generate-task` | 生成 Windows 任务计划 XML |
| `generate-config` | 生成默认配置文件（已存在需 `--force`） |
| `completions` | 生成 shell 补全脚本 (bash/zsh/fish) |

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

详见 `docs/configuration.md`

## 开发路线图

- [x] Phase 0: 脚手架、pyproject、CI、pre-commit
- [x] Phase 1: 配置管理、路径解析、日志系统
- [x] Phase 2: 头部注入器、模型探测、账号池、清理器
- [x] Phase 3: 守护进程模式
- [x] Phase 4: CLI 界面整合
- [x] Phase 5: 文档与补全生成
- [x] Phase 6: 测试与二进制打包
- [ ] Phase 7: 发布（流程见 `docs/release.md`）

## 许可证

MIT License