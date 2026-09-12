"""OpenCode official-CLI compatible request header generation."""

from __future__ import annotations

from .config import HeadersConfig


class HeaderInjector:
    """Generate OpenCode official CLI compatible request headers"""

    ZEN_ENDPOINT = "https://opencode.ai/zen/v1/chat/completions"

    def __init__(self, config: HeadersConfig, version: str = "unknown"):
        self.config = config
        self.version = version

    def build_headers(self, model: str | None = None, token: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self.config.user_agent.format(version=self.version),
            "x-opencode-client": self.config.x_opencode_client,
            "x-opencode-version": self.config.x_opencode_version.format(version=self.version),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if model:
            headers["x-model"] = model
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def to_env_export(self, model: str | None = None) -> str:
        """Generate shell export statements for eval"""
        h = self.build_headers(model)
        lines = []
        for k, v in h.items():
            env_key = k.upper().replace("-", "_")
            lines.append(f'export {env_key}="{v}"')
        return "\n".join(lines)

    def to_curl_args(self, model: str | None = None) -> str:
        """Generate curl header arguments"""
        h = self.build_headers(model)
        args = []
        for k, v in h.items():
            args.append(f'-H "{k}: {v}"')
        return " ".join(args)
