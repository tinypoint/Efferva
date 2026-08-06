import posixpath
from dataclasses import dataclass


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

    def __post_init__(self) -> None:
        if not posixpath.isabs(self.workspace_path):
            raise ValueError("workspace path must be absolute")
        if posixpath.commonpath((self.identity.home_path, self.workspace_path)) != (
            self.identity.home_path
        ):
            raise ValueError("workspace path must be inside the sandbox home")


@dataclass(frozen=True, slots=True)
class EffervaConfig:
    database_url: str
    sandbox: SandboxLayout
