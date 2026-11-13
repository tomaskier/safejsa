"""Load config.yaml, let environment variables override it, and hold the secrets.

Secrets are `SecretStr`, so they never show up in logs, reprs or traces by accident.
This module imports nothing else from the package (see SPEC.md, Dependency rules).

Environment overrides use the prefix `SAFEJSA_` and a double underscore between levels:
`SAFEJSA_HARNESS=v2`, `SAFEJSA_LLM__MODEL=qwen3.5:9b`, `SAFEJSA_EMAIL__APP_PASSWORD=...`.
List values such as `SAFEJSA_EMAIL__ALLOWED_SENDERS` are comma-separated.
`SAFEJSA_CONFIG` points to a config file other than ./config.yaml.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

ENV_PREFIX = "SAFEJSA_"
DEFAULT_PATH = Path("config.yaml")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMConfig(_Section):
    base_url: str = "http://localhost:11434/v1"
    api_key: SecretStr = SecretStr("local")
    model: str = "qwen3.5:4b"
    temperature: float = 0.2
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = "none"


class EmailConfig(_Section):
    imap_host: str = "imap.gmail.com"
    smtp_host: str = "smtp.gmail.com"
    address: str = ""
    app_password: SecretStr = SecretStr("")
    allowed_senders: list[str] = Field(default_factory=list)
    poll_seconds: int = 120

    @field_validator("allowed_senders", mode="before")
    @classmethod
    def _split_comma_list(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


class LangSmithConfig(_Section):
    api_key: SecretStr = SecretStr("")
    project: str = "safejsa"


class Config(_Section):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    harness: str = "v1"
    best_harness: str = "v1"
    email: EmailConfig = Field(default_factory=EmailConfig)
    langsmith: LangSmithConfig = Field(default_factory=LangSmithConfig)


class ConfigError(ValueError):
    """The config file or an environment override is invalid."""


def load_config(path: Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Read the config file (if it exists), apply environment overrides, and validate.

    A missing file is not an error: defaults apply, and secrets stay empty.
    """
    env = os.environ if env is None else env
    path = path or Path(env.get(f"{ENV_PREFIX}CONFIG", DEFAULT_PATH))

    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
        if loaded is not None and not isinstance(loaded, dict):
            raise ConfigError(f"{path} must hold a mapping of settings")
        data = loaded or {}

    for name, value in env.items():
        if not name.startswith(ENV_PREFIX) or name == f"{ENV_PREFIX}CONFIG":
            continue
        keys = name[len(ENV_PREFIX) :].lower().split("__")
        target = data
        for key in keys[:-1]:
            target = target.setdefault(key, {})
            if not isinstance(target, dict):
                raise ConfigError(f"{name} overrides a setting that is not a section")
        target[keys[-1]] = value

    try:
        return Config.model_validate(data)
    except ValueError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc
