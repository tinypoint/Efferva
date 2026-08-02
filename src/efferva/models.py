from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from efferva.identity import Capability


class SessionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class Session(BaseModel):
    id: UUID
    tenant_id: str
    owner_issuer: str
    owner_subject: str
    name: str
    status: str
    codex_version: str
    codex_runtime_sha256: str
    last_active_at: datetime
    created_at: datetime
    updated_at: datetime


class PromptInput(BaseModel):
    prompt: str = Field(min_length=1, max_length=1_000_000)


class PrincipalView(BaseModel):
    tenant_id: str
    issuer: str
    subject: str
    capabilities: list[Capability]


class AgUiMessage(BaseModel):
    id: str
    role: str
    content: str | list[dict[str, Any]] | None = None


class RunAgentInput(BaseModel):
    thread_id: str = Field(alias="threadId", min_length=1, max_length=200)
    run_id: str | None = Field(
        default=None,
        alias="runId",
        min_length=1,
        max_length=200,
    )
    messages: list[AgUiMessage] = Field(default_factory=list)
    state: Any = None
    tools: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    context: list[dict[str, Any]] = Field(default_factory=list)
    forwarded_props: Any = Field(default=None, alias="forwardedProps")
    resume: list[dict[str, Any]] | None = None

    model_config = {"populate_by_name": True}


class CodexControlInput(BaseModel):
    run_id: str | None = Field(
        default=None,
        alias="runId",
        min_length=1,
        max_length=200,
    )
    action: Literal[
        "plan.enable",
        "goal.get",
        "goal.clear",
        "goal.status",
        "goal.set",
    ]
    status: Literal["active", "paused"] | None = None
    objective: str | None = Field(default=None, min_length=1, max_length=1_000_000)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    reasoning_effort: str | None = Field(
        default=None,
        alias="reasoningEffort",
        min_length=1,
        max_length=50,
    )

    model_config = {"populate_by_name": True}
