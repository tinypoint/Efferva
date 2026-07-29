from functools import lru_cache
from pathlib import Path

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
