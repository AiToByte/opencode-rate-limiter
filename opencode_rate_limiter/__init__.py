"""OpenCode Rate Limiter - rate-limit mitigation and account-pool management.

The public API is re-exported here; the implementation lives in focused
modules (`config`, `paths`, `prober`, `pool`, `cleanup`, `daemon`, `cli`, ...).
"""

from __future__ import annotations

from .cleanup import CleanupManager, CleanupResult
from .cli import (
    COMMAND_HANDLERS,
    cmd_check,
    cmd_completions,
    cmd_daemon,
    cmd_daemon_stop,
    cmd_deep,
    cmd_diagnose,
    cmd_explain,
    cmd_generate_config,
    cmd_generate_launchd,
    cmd_generate_systemd,
    cmd_generate_task,
    cmd_headers,
    cmd_probe,
    cmd_quick,
    cmd_rotate,
    main,
)
from .completions import generate_completions
from .config import (
    FREE_MODELS,
    AccountPoolConfig,
    CleanupConfig,
    Config,
    DaemonConfig,
    HeadersConfig,
    ProberConfig,
)
from .daemon import (
    DaemonLockError,
    DaemonStatus,
    RateLimiterDaemon,
    get_daemon_lock_path,
    get_daemon_state_path,
    load_daemon_state,
    write_daemon_state,
)
from .diagnostics import Diagnosis, Finding, format_report, run_diagnostics
from .errors import (
    ClassifyResult,
    ErrorKind,
    classify_http,
    classify_opencode_log_line,
    classify_transport,
    explain_kind,
)
from .headers import HeaderInjector
from .logs import HumanFormatter, JSONFormatter, level_from_args, setup_logging
from .meta import __version__
from .parser import build_parser, should_print_banner
from .paths import (
    clear_opencode_version_cache,
    get_opencode_auth_files,
    get_opencode_config_dirs,
    get_opencode_native_cache_dirs,
    get_opencode_native_state_files,
    get_opencode_version,
)
from .pool import (
    Account,
    AccountHealth,
    AccountPool,
    build_auth_payload,
    credential_fingerprint,
    extract_access_token,
    extract_credential,
)
from .prober import ModelProber, ProbeResult
from .service import (
    generate_launchd_plist,
    generate_systemd_unit,
    generate_task_xml,
)

__all__ = [
    "COMMAND_HANDLERS",
    "FREE_MODELS",
    "Account",
    "AccountHealth",
    "AccountPool",
    "AccountPoolConfig",
    "ClassifyResult",
    "CleanupConfig",
    "CleanupManager",
    "CleanupResult",
    "Config",
    "DaemonConfig",
    "DaemonLockError",
    "DaemonStatus",
    "Diagnosis",
    "ErrorKind",
    "Finding",
    "HeaderInjector",
    "HeadersConfig",
    "HumanFormatter",
    "JSONFormatter",
    "ModelProber",
    "ProbeResult",
    "ProberConfig",
    "RateLimiterDaemon",
    "__version__",
    "build_auth_payload",
    "build_parser",
    "classify_http",
    "classify_opencode_log_line",
    "classify_transport",
    "clear_opencode_version_cache",
    "cmd_check",
    "cmd_completions",
    "cmd_daemon",
    "cmd_daemon_stop",
    "cmd_deep",
    "cmd_diagnose",
    "cmd_explain",
    "cmd_generate_config",
    "cmd_generate_launchd",
    "cmd_generate_systemd",
    "cmd_generate_task",
    "cmd_headers",
    "cmd_probe",
    "cmd_quick",
    "cmd_rotate",
    "credential_fingerprint",
    "explain_kind",
    "extract_access_token",
    "extract_credential",
    "format_report",
    "generate_completions",
    "generate_launchd_plist",
    "generate_systemd_unit",
    "generate_task_xml",
    "get_daemon_lock_path",
    "get_daemon_state_path",
    "get_opencode_auth_files",
    "get_opencode_config_dirs",
    "get_opencode_native_cache_dirs",
    "get_opencode_native_state_files",
    "get_opencode_version",
    "level_from_args",
    "load_daemon_state",
    "main",
    "run_diagnostics",
    "setup_logging",
    "should_print_banner",
    "write_daemon_state",
]
