from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SandboxLayout:
    session_volume_path: str = "/home/sandbox"
    workspace_path: str = "/home/sandbox/workspace"
    codex_home_path: str = "/home/sandbox/.codex"
    codex_runtime_dir: str = "/opt/efferva/runtimes"
    uid: int = 1000
    gid: int = 1000


@dataclass(frozen=True, slots=True)
class CodexConfig:
    api_key: str | None = None
    openai_base_url: str | None = None
    appserver_port: int = 4500


@dataclass(frozen=True, slots=True)
class EffervaConfig:
    database_url: str
    sandbox: SandboxLayout = field(default_factory=SandboxLayout)
    codex: CodexConfig = field(default_factory=CodexConfig)
