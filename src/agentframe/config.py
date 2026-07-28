from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="AGENTFRAME_",
        extra="ignore",
    )

    database_url: str = "postgresql://agentframe:agentframe@localhost:5432/agentframe"
    runtime_binary: Path = Path("agentframe-codex-runtime")
    codex_openai_base_url: str | None = None
    codex_model: str | None = None
    sandbox_backend: str = "docker"
    sandbox_image: str = "agentframe-sandbox:local"
    sandbox_port: int = 8081
    sandbox_cpu_limit: str = "2"
    docker_sandbox_memory_limit: str = "2g"
    kubernetes_sandbox_memory_limit: str = "2Gi"
    sandbox_pids_limit: int = 512
    workspace_path: str = "/workspace"
    docker_network: str = "agentframe"
    kubernetes_namespace: str = "default"
    kubernetes_storage_class: str | None = None
    kubernetes_workspace_size: str = "5Gi"
    public_base_url: str = "http://localhost:8080"
    instance_id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex)
    worker_poll_seconds: float = 0.25
    lease_ttl_seconds: int = 30
    lease_renew_seconds: int = 10
    max_parallel_threads_per_session: int = 4
    max_parallel_runs_per_instance: int = 16


@lru_cache
def get_settings() -> Settings:
    return Settings()
