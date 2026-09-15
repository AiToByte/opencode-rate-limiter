"""CLI argument parser and structured-output (banner suppression) rules."""

from __future__ import annotations

import argparse
from pathlib import Path

from .meta import __version__

_STRUCTURED_COMMANDS = frozenset(
    {
        "check",
        "diagnose",
        "probe",
        "headers",
        "explain",
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
    "--json-verbose",
    "--verbose",
    "--quiet",
    "--dry-run",
    "--log-file",
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
  opencode-rate-limiter quick                    # Back up auth.json
  opencode-rate-limiter deep                     # Deep cleanup
  opencode-rate-limiter probe all --json         # Probe all models
  opencode-rate-limiter headers --export         # Export headers for curl
  opencode-rate-limiter daemon --interval 60     # Run as daemon
  opencode-rate-limiter check --json             # Health check
        """,
    )
    parser.add_argument("--config", type=Path, help="配置文件路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式日志")
    parser.add_argument("--json-verbose", action="store_true", help="JSON 日志附带代码位置字段")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="详细输出 (-v, -vv)")
    parser.add_argument("-q", "--quiet", action="store_true", help="仅错误输出")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不修改文件")
    parser.add_argument("--log-file", type=Path, default=None, help="追加日志到文件（自动轮转）")
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
    common.add_argument(
        "--log-file", type=Path, default=argparse.SUPPRESS, help="追加日志到文件（自动轮转）"
    )
    common.add_argument(
        "--json-verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="JSON 日志附带代码位置字段",
    )

    def add_sub(name: str, help_text: str) -> argparse.ArgumentParser:
        """Register a subcommand, mirroring `help` into `description` so both -h
        output and completion generation can read the one-line summary."""
        return sub.add_parser(name, parents=[common], help=help_text, description=help_text)

    add_sub("quick", "快速维护：备份 auth.json（本地操作不解除服务端限额）")
    add_sub("deep", "深度维护：quick + 清缓存")

    probe_p = add_sub("probe", "探测模型可用性")
    probe_p.add_argument("model", nargs="?", default="all", help="模型名称或 all")

    headers_p = add_sub("headers", "输出官方 CLI 兼容请求头")
    headers_p.add_argument("--model", help="目标模型（可选）")
    headers_p.add_argument("--export", action="store_true", help="输出 shell export 格式")

    rotate_p = add_sub("rotate", "手动轮换账号池")
    rotate_p.add_argument(
        "--strategy",
        choices=["round_robin", "least_used", "health"],
        default=None,
        help="选择策略；省略时使用配置文件中的 strategy",
    )
    rotate_p.add_argument(
        "--to",
        metavar="NAME",
        default=None,
        help="直接选中指定账号（大小写敏感），跳过策略选择",
    )
    rotate_p.add_argument(
        "--apply",
        action="store_true",
        help="把选中账号的 auth JSON 写入 OpenCode auth.json（先备份；需搭配 --dry-run 预览）",
    )

    check_p = add_sub("check", "健康检查聚合输出")
    check_p.add_argument("--trend", action="store_true", help="附显示探测趋势网格与最近事件")
    check_p.add_argument(
        "--export-events", type=Path, default=None, help="把决策事件环导出到文件后退出"
    )
    check_p.add_argument(
        "--export-format",
        choices=["jsonl", "csv"],
        default="jsonl",
        help="事件导出格式（默认 jsonl）",
    )

    diag_p = add_sub("diagnose", "Zen 限额诊断（出口 IP / 代理 / 错误层级 / 建议）")
    diag_p.add_argument("--model", help="探测的模型（默认取配置列表第一个；仅消耗 1 次配额）")
    diag_p.add_argument(
        "--from-text", default=None, help="离线分类一条报错文本（零配额，不发探测）"
    )
    diag_p.add_argument(
        "--from-log", type=Path, default=None, help="离线分类日志文件（零配额，不发探测）"
    )

    explain_p = add_sub("explain", "解释一条 opencode 报错（离线分类，零配额）")
    explain_p.add_argument("text", nargs="?", default=None, help="要分类的报错文本")
    explain_p.add_argument(
        "--from-log", type=Path, default=None, help="从日志文件逐行分类（零配额）"
    )

    daemon_p = add_sub("daemon", "后台守护进程模式")
    daemon_p.add_argument("--interval", type=int, default=None, help="探测间隔（秒，最小 5）")
    daemon_p.add_argument("--models", type=str, help="逗号分隔的模型列表")
    daemon_mode = daemon_p.add_mutually_exclusive_group()
    daemon_mode.add_argument(
        "--once", action="store_true", help="只跑一轮探测就退出（cron/任务计划友好）"
    )
    daemon_mode.add_argument("--stop", action="store_true", help="优雅停止正在运行的守护进程后退出")

    add_sub("generate-systemd", "生成 systemd 服务文件")
    add_sub("generate-launchd", "生成 launchd plist")
    add_sub("generate-task", "生成 Windows 任务计划 XML")

    gen_config_p = add_sub("generate-config", "生成默认配置文件（已存在时需 --force 覆盖）")
    gen_config_p.add_argument("--force", action="store_true", help="覆盖已存在的配置文件")

    completions_p = add_sub("completions", "生成 shell 补全脚本 (bash/zsh/fish/powershell)")
    completions_p.add_argument(
        "shell", choices=["bash", "zsh", "fish", "powershell"], help="目标 shell"
    )

    return parser
