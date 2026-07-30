from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, TypeAlias
from uuid import UUID

from efferva.sandbox import SandboxEnvironment


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Trusted execution context supplied by Efferva, never by the model."""

    thread_id: str
    turn_id: str
    call_id: str
    sandbox: SandboxEnvironment
    run_id: UUID | None = None
    app_thread_id: UUID | None = None
    session_id: UUID | None = None
    tenant_id: str | None = None
    owner_issuer: str | None = None
    owner_subject: str | None = None
    worker_owner_id: str | None = None
    fencing_epoch: int | None = None


ToolHandler: TypeAlias = Callable[
    [ToolContext, Mapping[str, Any]],
    Any | Awaitable[Any],
]


@dataclass(frozen=True, slots=True)
class Tool:
    """An application-side tool exposed to Codex through dynamicTools."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: ToolHandler
    defer_loading: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Tool name must not be empty")
        if not self.description.strip():
            raise ValueError(f"Tool {self.name!r} description must not be empty")
        if self.input_schema.get("type") != "object":
            raise ValueError(f"Tool {self.name!r} input_schema must describe an object")
        if not callable(self.handler):
            raise TypeError(f"Tool {self.name!r} handler must be callable")

    def codex_spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "inputSchema": deepcopy(dict(self.input_schema)),
            "deferLoading": self.defer_loading,
        }

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> Any:
        result = self.handler(context, arguments)
        if inspect.isawaitable(result):
            return await result
        return result


def tool_result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
