"""OpenCode official-CLI compatible request header generation."""

from __future__ import annotations

from typing import Any

from .config import HeadersConfig


def _safe_format(template: str, args: dict[str, Any]) -> str:
    """Format a header template, falling back to the raw template on misuse."""
    try:
        return template.format(**args)
    except (KeyError, IndexError, ValueError):
        return template


def _shell_escape(value: str) -> str:
    """Escape a value for inclusion in a double-quoted shell string."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")


class HeaderInjector:
    """Generate OpenCode official CLI compatible request headers"""

    ZEN_ENDPOINT = "https://opencode.ai/zen/v1/chat/completions"

    def __init__(self, config: HeadersConfig, version: str = "unknown"):
        self.config = config
        self.version = version

    def build_headers(
        self,
        model: str | None = None,
        token: str | None = None,
        session: str | None = None,
    ) -> dict[str, str]:
        format_args = {"version": self.version, "model": model or ""}
        headers = {
            "User-Agent": _safe_format(self.config.user_agent, format_args),
            "x-opencode-client": _safe_format(self.config.x_opencode_client, format_args),
            "x-opencode-version": _safe_format(self.config.x_opencode_version, format_args),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if model:
            headers["x-model"] = model
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if session:
            # Required by the Zen gateway (MissingSessionID otherwise);
            # mirrors the official CLI's x-opencode-session header.
            headers["x-opencode-session"] = session
        return headers

    def to_env_export(self, model: str | None = None, session: str | None = None) -> str:
        """Generate shell export statements for eval (values safely escaped)."""
        h = self.build_headers(model, session=session)
        lines = []
        for k, v in h.items():
            env_key = k.upper().replace("-", "_")
            lines.append(f'export {env_key}="{_shell_escape(v)}"')
        return "\n".join(lines)

    def to_curl_args(self, model: str | None = None) -> str:
        """Generate curl header arguments"""
        h = self.build_headers(model)
        args = []
        for k, v in h.items():
            args.append(f'-H "{k}: {v}"')
        return " ".join(args)
