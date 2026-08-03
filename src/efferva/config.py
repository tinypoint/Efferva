from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="EFFERVA_",
        extra="ignore",
    )

    database_url: str | None = None
    codex_version: str = "0.146.0"
    codex_release_target: str | None = None
    codex_archive_sha256: str | None = None
    codex_release_cache_dir: Path = Path("/tmp/efferva-codex")
    codex_openai_base_url: str | None = None
    sandbox_image: str = "python:3.13-slim-bookworm"
    sandbox_cpu_limit: str = "2"
    sandbox_memory_limit: str = "2g"
    sandbox_uid: int = 1000
    sandbox_gid: int = 1000
    opensandbox_server_url: str | None = None
    opensandbox_api_key: str | None = None
    opensandbox_use_server_proxy: bool = True
    opensandbox_credential_proxy_enabled: bool = True
    session_volume_path: str = "/home/sandbox"
    workspace_path: str = "/home/sandbox/workspace"
    codex_home_path: str = "/home/sandbox/.codex"
    codex_runtime_dir: str = "/opt/efferva/runtimes"
    codex_appserver_port: int = 4500
    session_volume_size: str = "10Gi"
    sandbox_idle_timeout_seconds: int = 12 * 60 * 60
    deleted_session_volume_retention_days: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
