import posixpath
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SandboxIdentity:
    username: str | None
    uid: int
    gid: int
    home_path: str

    def __post_init__(self) -> None:
        if self.username is not None and not self.username.strip():
            raise ValueError("sandbox username cannot be empty")
        if self.uid < 0 or self.gid < 0:
            raise ValueError("sandbox UID and GID must be non-negative")
        if not posixpath.isabs(self.home_path):
            raise ValueError("sandbox home path must be absolute")


@dataclass(frozen=True, slots=True)
class SandboxLayout:
    identity: SandboxIdentity
    workspace_path: str
    codex_home_path: str
    codex_runtime_dir: str = "/opt/efferva/runtimes"

    def __post_init__(self) -> None:
        for name, path in (
            ("workspace", self.workspace_path),
            ("Codex home", self.codex_home_path),
            ("Codex runtime", self.codex_runtime_dir),
        ):
            if not posixpath.isabs(path):
                raise ValueError(f"{name} path must be absolute")
        for name, path in (
            ("workspace", self.workspace_path),
            ("Codex home", self.codex_home_path),
        ):
            if posixpath.commonpath((self.identity.home_path, path)) != (
                self.identity.home_path
            ):
                raise ValueError(f"{name} path must be inside the sandbox home")


@dataclass(frozen=True, slots=True)
class CodexConfig:
    api_key: str | None = None
    openai_base_url: str | None = None
    appserver_port: int = 4500


@dataclass(frozen=True, slots=True)
class EffervaConfig:
    database_url: str
    sandbox: SandboxLayout
    codex: CodexConfig = field(default_factory=CodexConfig)
