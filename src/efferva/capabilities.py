from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SkillRoot:
    """A sandbox-local capability root containing Codex skills.

    Codex discovers ``SKILL.md`` files through the sandbox executor. The path
    therefore belongs to the sandbox image or the Session workspace, not the
    Efferva application container.
    """

    id: str
    path: str
    enabled_by_default: bool = True

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("SkillRoot id must not be empty")
        if not self.path.startswith("/"):
            raise ValueError(f"SkillRoot {self.id!r} path must be absolute")

    def codex_spec(self, environment_id: str) -> dict[str, object]:
        return {
            "id": self.id,
            "location": {
                "type": "environment",
                "environmentId": environment_id,
                "path": self.path,
            },
        }
