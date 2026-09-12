# 发布流程（Release Checklist）

适用于 0.3.0 及以后。发布由 **tag 驱动**：推送 `v*` tag 后
`.github/workflows/release.yml` 自动完成版本校验 → 测试 → 三平台二进制 →
GitHub Release（含 SHA256SUMS）→ PyPI 发布。

---

## 1. 发布前检查清单（手工）

- [ ] `uv run pytest -q` 全绿；`uv run ruff check .` / `uv run mypy .` 通过
- [ ] CHANGELOG：`[Unreleased]` 内容归并为 `[0.3.0] - <日期>`，破坏性变更显著标注
- [ ] 四份 `docs/*.md` 头部版本号与 MANUAL 头部版本号同步
- [ ] `uv run python scripts/check_version.py 0.3.0` 三处版本号一致
      （meta.py / pyproject.toml / man 页）
- [ ] `docs/roadmap.md` 基线说明更新
- [ ] 路线图中该迭代的验收标准逐条勾掉

## 2. 版本号提升（三处 + 文档头）

```bash
# opencode_rate_limiter/meta.py
# pyproject.toml
# man/opencode-rate-limiter.1
# MANUAL.md + docs/*.md 头部
uv run python scripts/check_version.py <new-version>   # 必须输出 All agree
```

## 3. 打 tag 发布

```bash
git add -A && git commit -m "release: v0.3.0"
git tag v0.3.0 && git push origin main --tags
```

流水线阶段（全部自动）：

| 阶段 | 内容 | 失败影响 |
|------|------|----------|
| check-version | `scripts/check_version.py` 断言三处版本一致且等于 tag 名 | 中止发布 |
| test | 快速测试门（ubuntu） | 中止发布 |
| build-binaries | 三平台 PyInstaller（入口 shim，产物 ~13 MB），每个二进制 `--version` + `check --json` 冒烟 | 中止发布 |
| github-release | 汇总产物 + SHA256SUMS.txt → GitHub Release（自动生成 notes） | 仅缺 Release 资产 |
| pypi-publish | `uv build` → PyPI（优先 Trusted Publishing；配置了 `PYPI_API_TOKEN` 则走 token） | 仅缺 PyPI 版本 |

## 4. 发布后验证

- [ ] GitHub Release 页面：3 个二进制 + `SHA256SUMS.txt`，notes 完整
- [ ] `sha256sum -c SHA256SUMS.txt` 本地校验通过
- [ ] 下载对应平台二进制：`--version` 输出正确版本
- [ ] PyPI 页面可用：`uvx opencode-rate-limiter@0.3.0 --version` /
      `pip install opencode-rate-limiter==0.3.0`
- [ ] 用户文档（README / docs / MANUAL）中的版本引用无残留旧版本

## 5. 一次性配置（首次发布前）

- **PyPI Trusted Publishing**：在 pypi.org → 项目 → Publishing 增加 GitHub
  workflow 绑定（owner/repo/workflow=`release.yml`，environment=`pypi`）。
  不使用 Trusted Publishing 也可在 repo secrets 配置 `PYPI_API_TOKEN`（自动切换
  token 路径）。
- GitHub 仓库需允许 GITHUB_TOKEN 创建 Release（默认允许）。

## 6. 热修复（hotfix）

1. 从发布 tag 拉 `hotfix/x.y.z+1` 分支修复
2. 走相同清单（版本号提到补丁号），tag `v0.3.1` 触发同一流水线
3. CHANGELOG 补 `[0.3.1]` 条目
