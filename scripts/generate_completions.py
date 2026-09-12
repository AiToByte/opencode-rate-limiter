#!/usr/bin/env python3
"""Generate shell completions for opencode-rate-limiter from the live CLI."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencode_rate_limiter import generate_completions


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    output_dir = project_root / "completion"
    output_dir.mkdir(exist_ok=True)
    for shell in ("bash", "zsh", "fish"):
        output_path = output_dir / f"opencode-rate-limiter.{shell}"
        output_path.write_text(generate_completions(shell), encoding="utf-8")
        print(f"Generated shell completion: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
