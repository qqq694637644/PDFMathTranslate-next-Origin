from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

ENV_FILE_VARIABLE = "PDF2ZH_ENV_FILE"
DEFAULT_ENV_FILE_NAME = ".env"


def resolve_env_file_path(value: str | Path | None = None) -> Path:
    """Resolve the shared dotenv file used by all personal GPT Actions commands."""
    configured = value or os.getenv(ENV_FILE_VARIABLE)
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / DEFAULT_ENV_FILE_NAME).resolve()


def pydantic_env_file_kwargs(value: str | Path | None = None) -> dict[str, object]:
    """Return constructor kwargs that make pydantic-settings read one dotenv file."""
    return {
        "_env_file": resolve_env_file_path(value),
        "_env_file_encoding": "utf-8",
    }


def read_env_file_values(value: str | Path | None = None) -> dict[str, str]:
    env_file = resolve_env_file_path(value)
    return {
        key: item for key, item in dotenv_values(env_file).items() if item is not None
    }


def resolve_path_from_env_file(
    value: str | Path,
    env_file: str | Path | None = None,
) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = resolve_env_file_path(env_file).parent / path
    return path.resolve()
