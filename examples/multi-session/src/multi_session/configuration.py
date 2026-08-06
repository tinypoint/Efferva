import os
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from efferva import (
    ClaudeCode,
    Codex,
    EffervaConfig,
    SandboxIdentity,
    SandboxLayout,
)
from efferva.sandbox.providers.opensandbox import (
    OpenSandboxConnectionConfig,
    OpenSandboxCreateSpec,
    OpenSandboxCredentialProxy,
)


@dataclass(frozen=True, slots=True)
class InstanceConfig:
    efferva: EffervaConfig
    engine: Codex | ClaudeCode
    sandbox_spec: OpenSandboxCreateSpec


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    opensandbox: OpenSandboxConnectionConfig
    codex: InstanceConfig
    claude: InstanceConfig


def load_config() -> ApplicationConfig:
    return ApplicationConfig(
        opensandbox=OpenSandboxConnectionConfig(
            server_url=_required("EFFERVA_OPENSANDBOX_SERVER_URL"),
            api_key=os.environ.get("EFFERVA_OPENSANDBOX_API_KEY") or None,
            use_server_proxy=_boolean("EFFERVA_OPENSANDBOX_USE_SERVER_PROXY", True),
        ),
        codex=_codex_config(),
        claude=_claude_config(),
    )


def _codex_config() -> InstanceConfig:
    layout = SandboxLayout(
        identity=SandboxIdentity(
            username="node",
            uid=1000,
            gid=1000,
            home_path="/home/node",
        ),
        workspace_path="/home/node/workspace",
    )
    api_key = os.environ.get("OPENAI_API_KEY") or None
    base_url = os.environ.get("EFFERVA_CODEX_OPENAI_BASE_URL") or None
    return InstanceConfig(
        efferva=EffervaConfig(
            database_url=_required("EFFERVA_CODEX_DATABASE_URL"),
            sandbox=layout,
        ),
        engine=Codex(api_key=api_key, base_url=base_url),
        sandbox_spec=OpenSandboxCreateSpec(
            image=_value(
                "EFFERVA_CODEX_SANDBOX_IMAGE",
                "mcr.microsoft.com/devcontainers/javascript-node:1-22-bookworm",
            ),
            cpu_limit=_value("EFFERVA_CODEX_SANDBOX_CPU_LIMIT", "2"),
            memory_limit=_value("EFFERVA_CODEX_SANDBOX_MEMORY_LIMIT", "2g"),
            session_volume_size=_value(
                "EFFERVA_CODEX_SESSION_VOLUME_SIZE",
                "10Gi",
            ),
            credential_proxy=_credential_proxy(
                credential=api_key,
                base_url=base_url or "https://api.openai.com/v1",
                enabled_name="EFFERVA_CODEX_CREDENTIAL_PROXY_ENABLED",
                auth_type="bearer",
            ),
            layout=layout,
        ),
    )


def _claude_config() -> InstanceConfig:
    layout = SandboxLayout(
        identity=SandboxIdentity(
            username=None,
            uid=1000,
            gid=1000,
            home_path="/home/sandbox",
        ),
        workspace_path="/home/sandbox/workspace",
    )
    api_key = _required("ANTHROPIC_API_KEY")
    base_url = os.environ.get("ANTHROPIC_BASE_URL") or None
    return InstanceConfig(
        efferva=EffervaConfig(
            database_url=_required("EFFERVA_CLAUDE_DATABASE_URL"),
            sandbox=layout,
        ),
        engine=ClaudeCode(
            api_key=api_key,
            base_url=base_url,
            model=os.environ.get("ANTHROPIC_MODEL") or None,
        ),
        sandbox_spec=OpenSandboxCreateSpec(
            image=_value(
                "EFFERVA_CLAUDE_SANDBOX_IMAGE",
                "python:3.13-slim-bookworm",
            ),
            cpu_limit=_value("EFFERVA_CLAUDE_SANDBOX_CPU_LIMIT", "2"),
            memory_limit=_value("EFFERVA_CLAUDE_SANDBOX_MEMORY_LIMIT", "2g"),
            session_volume_size=_value(
                "EFFERVA_CLAUDE_SESSION_VOLUME_SIZE",
                "10Gi",
            ),
            credential_proxy=_credential_proxy(
                credential=api_key,
                base_url=base_url or "https://api.anthropic.com",
                enabled_name="EFFERVA_CLAUDE_CREDENTIAL_PROXY_ENABLED",
                auth_type="api_key",
            ),
            layout=layout,
        ),
    )


def _credential_proxy(
    *,
    credential: str | None,
    base_url: str,
    enabled_name: str,
    auth_type: Literal["bearer", "api_key"],
) -> OpenSandboxCredentialProxy | None:
    if not credential or not _boolean(enabled_name, True):
        return None
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("Engine base URL must be an HTTP(S) URL")
    default_port = 443 if parsed.scheme == "https" else 80
    if parsed.port not in {None, default_port}:
        raise RuntimeError(
            f"{enabled_name}=true requires a default-port engine base URL"
        )
    if auth_type == "bearer":
        return OpenSandboxCredentialProxy(
            credential=credential,
            host=parsed.hostname,
            scheme=parsed.scheme,
            auth_type="bearer",
        )
    return OpenSandboxCredentialProxy(
        credential=credential,
        host=parsed.hostname,
        scheme=parsed.scheme,
        auth_type="api_key",
        header_name="x-api-key",
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
