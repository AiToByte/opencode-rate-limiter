> **注：本文档为早期拆分文档，已停止维护。** 部分内容属于当时的设计规划，
> 与当前实现存在出入（差异清单见 MANUAL.md 第 12 节）。
> **权威文档请以 [MANUAL.md](../MANUAL.md) 为准。**

# 发布检查清单 (Phase 7)

版本发布前逐项核对。完成全部步骤后，发布 GitHub Release 即可。

## 1. 版本一致性

- [ ] `opencode_rate_limiter.py` 顶部 `__version__ == "0.1.0"` 与
      `pyproject.toml` 的 `[project] version` 一致
- [ ] `pyproject.toml` 同步 `pip install` 依赖与 `optional-dependencies`
- [ ] `man/opencode-rate-limiter.1` 与新命令/选项一致

## 2. 质量门禁（本地验证）

```bash
uv sync --dev --all-extras
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest --cov=. --cov-report=term-missing
uv run pre-commit run --all-files
```

- [ ] 全部通过，0 skipped
- [ ] 覆盖率达标（核心模块）

## 3. 二进制产物

每平台执行一次：

```bash
pip install ".[build]"
python scripts/build_binary.py
```

- [ ] 产物进入 `dist/`，命名含平台标签
- [ ] 构建脚本自带 `--help` 冒烟通过
- [ ] 手动抽查：`--version`、`completions bash`、`check --json`

## 4. 功能冒烟（真实环境）

- [ ] `quick` / `deep`（dry-run 先跑一遍）
- [ ] `probe all` 真实返回模型状态
- [ ] `headers --export` 可被 `eval` 与 curl 使用
- [ ] `daemon --interval 60` 运行一个周期并优雅退出
- [ ] `check --json` 输出含 `daemon` 运行状态

## 5. CI / 发布

- [ ] 推送 `main` 后 `.github/workflows/ci.yml` 三个 job 全绿
- [ ] `build-binary` 三平台产物已上传
- [ ] 创建 GitHub Release（`gh release create v0.1.0 --generate-notes`）
- [ ] Release 作业生成 `SHA256SUMS.txt` 并附带全部二进制

## 6. 文档回归

- [ ] `README.md` 状态、命令表、快速开始与本地行为一致
- [ ] `docs/*` 命令示例沿用最新 CLI
- [ ] `CHANGELOG.md` 记录本次改动