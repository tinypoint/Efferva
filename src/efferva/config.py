import tomllib
from collections.abc import Mapping
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="EFFERVA_",
        extra="ignore",
    )

    database_url: str = "postgresql://efferva:efferva@localhost:5432/efferva"
    runtime_binary: Path | None = None
    codex_config_file: Path | None = None
    codex_openai_base_url: str | None = None
    codex_model: str | None = None
    sandbox_provider: str = "opensandbox"
    sandbox_image: str = "ghcr.io/openai/codex-universal:latest"
    sandbox_cpu_limit: str = "2"
    sandbox_memory_limit: str = "2g"
    opensandbox_server_url: str = "http://localhost:8080"
    opensandbox_api_key: str | None = None
    opensandbox_use_server_proxy: bool = True
    workspace_path: str = "/workspace"
    public_base_url: str = "http://localhost:8080"
    instance_id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex)
    worker_poll_seconds: float = 0.25
    lease_ttl_seconds: int = 30
    lease_renew_seconds: int = 10
    max_parallel_threads_per_session: int = 4
    max_parallel_runs_per_instance: int = 16
    executor_gateway_host: str = "127.0.0.1"
    executor_gateway_port: int = 0


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_codex_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    with path.open("rb") as stream:
        value = tomllib.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Codex config must be a TOML table: {path}")
    return value


def merge_codex_config(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
) -> dict[str, Any]:
    merged = deepcopy(dict(base))
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            merged[key] = merge_codex_config(existing, value)
        else:
            merged[key] = deepcopy(value)
    return merged
