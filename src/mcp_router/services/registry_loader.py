"""MCP registry loader and env substitution."""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from mcp_router.models.registry import Registry

logger = logging.getLogger(__name__)


def load_env_file(env_path: Path | None = None) -> dict[str, str]:
    if env_path is None:
        env_path = Path.cwd() / ".env"
    if not env_path.exists():
        return {}
    env_vars: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        env_vars[key.strip()] = value
    return env_vars


def substitute_env_vars(config: Any, env_vars: dict[str, str] | None = None) -> Any:
    if env_vars is None:
        env_vars = load_env_file()
        env_vars.update(os.environ)

    if isinstance(config, dict):
        return {k: substitute_env_vars(v, env_vars) for k, v in config.items()}
    if isinstance(config, list):
        return [substitute_env_vars(item, env_vars) for item in config]
    if isinstance(config, str):
        pattern = re.compile(r"\$\{([^}]+)\}")

        def repl(match: re.Match[str]) -> str:
            name = match.group(1)
            return env_vars.get(name, match.group(0))

        return pattern.sub(repl, config)
    return config


def load_registry(path: Path) -> Registry:
    if not path.exists():
        logger.warning("Registry not found: %s", path)
        return Registry.empty()
    raw = json.loads(path.read_text(encoding="utf-8"))
    substituted = substitute_env_vars(raw)
    return Registry.model_validate(substituted)
