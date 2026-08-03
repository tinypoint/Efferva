from dataclasses import dataclass
import os
from urllib.parse import urlparse

from efferva import CodexConfig, EffervaConfig, SandboxLayout
from efferva.sandbox.providers.opensandbox import (
    OpenSandboxConnectionConfig,
    OpenSandboxCreateSpec,
    OpenSandboxCredentialProxy,
)


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    efferva: EffervaConfig
    opensandbox_connection: OpenSandboxConnectionConfig
    sandbox_spec: OpenSandboxCreateSpec


def load_config() -> ApplicationConfig:
    layout = SandboxLayout(
        session_volume_path=_value(
            "EFFERVA_SESSION_VOLUME_PATH",
            "/home/sandbox",
        ),
        workspace_path=_value(
            "EFFERVA_WORKSPACE_PATH",
            "/home/sandbox/workspace",
        ),
        codex_home_path=_value(
            "EFFERVA_CODEX_HOME_PATH",
            "/home/sandbox/.codex",
        ),
        codex_runtime_dir=_value(
            "EFFERVA_CODEX_RUNTIME_DIR",
            "/opt/efferva/runtimes",
        ),
        uid=int(_value("EFFERVA_SANDBOX_UID", "1000")),
        gid=int(_value("EFFERVA_SANDBOX_GID", "1000")),
    )
    api_key = os.environ.get("OPENAI_API_KEY") or None
    base_url = os.environ.get("EFFERVA_CODEX_OPENAI_BASE_URL") or None
    credential_proxy = _credential_proxy(
        api_key=api_key,
        base_url=base_url,
        enabled=_boolean(
            "EFFERVA_OPENSANDBOX_CREDENTIAL_PROXY_ENABLED",
            True,
        ),
    )
    return ApplicationConfig(
        efferva=EffervaConfig(
            database_url=_required("EFFERVA_DATABASE_URL"),
            sandbox=layout,
            codex=CodexConfig(
                api_key=api_key,
                openai_base_url=base_url,
                appserver_port=int(
                    _value("EFFERVA_CODEX_APPSERVER_PORT", "4500")
                ),
            ),
        ),
        opensandbox_connection=OpenSandboxConnectionConfig(
            server_url=_required("EFFERVA_OPENSANDBOX_SERVER_URL"),
            api_key=os.environ.get("EFFERVA_OPENSANDBOX_API_KEY") or None,
            use_server_proxy=_boolean(
                "EFFERVA_OPENSANDBOX_USE_SERVER_PROXY",
                True,
            ),
        ),
        sandbox_spec=OpenSandboxCreateSpec(
            image=_value(
                "EFFERVA_SANDBOX_IMAGE",
                "mcr.microsoft.com/devcontainers/javascript-node:1-22-bookworm",
            ),
            cpu_limit=_value("EFFERVA_SANDBOX_CPU_LIMIT", "2"),
            memory_limit=_value("EFFERVA_SANDBOX_MEMORY_LIMIT", "2g"),
            session_volume_size=_value("EFFERVA_SESSION_VOLUME_SIZE", "10Gi"),
            credential_proxy=credential_proxy,
        ),
    )


def _credential_proxy(
    *,
    api_key: str | None,
    base_url: str | None,
    enabled: bool,
) -> OpenSandboxCredentialProxy | None:
    if not enabled or not api_key:
        return None
    parsed = urlparse(base_url or "https://api.openai.com/v1")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    default_port = 443 if parsed.scheme == "https" else 80
    if parsed.port not in {None, default_port}:
        return None
    return OpenSandboxCredentialProxy(
        bearer_token=api_key,
        host=parsed.hostname,
        scheme=parsed.scheme,
    )


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    raise RuntimeError(f"{name} is required")


def _value(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _boolean(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean")
