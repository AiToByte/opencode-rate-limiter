"""CLI argument parser and structured-output (banner suppression) rules."""

from __future__ import annotations

import argparse
from pathlib import Path

from .meta import __version__

_STRUCTURED_COMMANDS = frozenset(
    {
        "check",
        "probe",
        "headers",
        "generate-systemd",
        "generate-launchd",
        "generate-task",
        "generate-config",
        "completions",
    }
)


_GLOBAL_FLAGS = {
    "--config",
    "--json",
    "--verbose",
    "--quiet",
    "--dry-run",
    "--version",
    "--help",
    "-h",
}


def should_print_banner(command: str, json_output: bool) -> bool:
    """Return True when the human-facing banner should be printed for a command."""
    return not json_output and command not in _STRUCTURED_COMMANDS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opencode-rate-limiter",
        description="OpenCode 免费模型限流缓解与账号池管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  opencode-rate-limiter quick                    # Quick rate limit relief
  opencode-rate-limiter deep                     # Deep cleanup
  opencode-rate-limiter probe all --json         # Probe all models
  opencode-rate-limiter headers --export         # Export headers for curl
  opencode-rate-limiter daemon --interval 60     # Run as daemon
  opencode-rate-limiter check --json             # Health check
        """,
    )
    parser.add_argument("--config", type=Path, help="配置文件路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式日志")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="详细输出 (-v, -vv)")
    parser.add_argument("-q", "--quiet", action="store_true", help="仅错误输出")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不修改文件")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", required=True, help="子命令")

    # Shared flags, available both before and after the subcommand.
    # SUPPRESS default lets parent-parser values survive when the child omits them.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="输出 JSON 格式"
    )
    common.add_argument(
        "--dry-run", action="store_true", default=argparse.SUPPRESS, help="预览模式，不修改文件"
    )

    def add_sub(name: str, help_text: str) -> argparse.ArgumentParser:
        """Register a subcommand, mirroring `help` into `description` so both -h
        output and completion generation can read the one-line summary."""
        return sub.add_parser(name, parents=[common], help=help_text, description=help_text)

    add_sub("quick", "快速解除限流（清理退避锁 + 重置 Token）")
    add_sub("deep", "深度清理（+ 清除缓存 + 强制重新登录）")

    probe_p = add_sub("probe", "探测模型可用性")
    probe_p.add_argument("model", nargs="?", default="all", help="模型名称或 all")

    headers_p = add_sub("headers", "输出官方 CLI 兼容请求头")
    headers_p.add_argument("--model", help="目标模型（可选）")
    headers_p.add_argument("--export", action="store_true", help="输出 shell export 格式")

    rotate_p = add_sub("rotate", "手动轮换账号池")
    rotate_p.add_argument(
        "--strategy", choices=["round_robin", "least_used", "health"], default="health"
    )
    rotate_p.add_argument(
        "--apply",
        action="store_true",
        help="把选中账号的 auth JSON 写入 OpenCode auth.json（先备份；需搭配 --dry-run 预览）",
    )

    add_sub("check", "健康检查聚合输出")

    daemon_p = add_sub("daemon", "后台守护进程模式")
    daemon_p.add_argument("--interval", type=int, default=None, help="探测间隔（秒，最小 5）")
    daemon_p.add_argument("--models", type=str, help="逗号分隔的模型列表")

    add_sub("generate-systemd", "生成 systemd 服务文件")
    add_sub("generate-launchd", "生成 launchd plist")
    add_sub("generate-task", "生成 Windows 任务计划 XML")

    gen_config_p = add_sub("generate-config", "生成默认配置文件（已存在时需 --force 覆盖）")
    gen_config_p.add_argument("--force", action="store_true", help="覆盖已存在的配置文件")

    completions_p = add_sub("completions", "生成 shell 补全脚本 (bash/zsh/fish)")
    completions_p.add_argument("shell", choices=["bash", "zsh", "fish"], help="目标 shell")

    return parser
