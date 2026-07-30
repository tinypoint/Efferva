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
    workspace_ref: str
    codex_version: str
    codex_runtime_sha256: str
    created_at: datetime
    updated_at: datetime


class ThreadCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    model_provider: str | None = Field(default=None, min_length=1, max_length=100)
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh", "ultra"] | None = None
    skill_roots: list[str] | None = None
    memory_mode: Literal["disabled", "enabled"] = "disabled"


class Thread(BaseModel):
    id: UUID
    session_id: UUID
    codex_thread_id: UUID | None
    title: str | None
    runtime_config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class RunCreate(BaseModel):
    prompt: str = Field(min_length=1)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh", "ultra"] | None = None


class ThreadGoalSet(BaseModel):
    objective: str = Field(min_length=1, max_length=10_000)
    token_budget: int | None = Field(default=None, gt=0)


class ThreadGoal(BaseModel):
    thread_id: str
    objective: str
    status: str
    token_budget: int | None = None
    tokens_used: int = 0
    time_used_seconds: int = 0
    created_at: int
    updated_at: int


class Run(BaseModel):
    id: UUID
    agui_run_id: str
    thread_id: UUID
    status: str
    input: dict[str, Any]
    codex_turn_id: str | None
    error: str | None
    last_seq: int
    created_at: datetime
    updated_at: datetime


class Artifact(BaseModel):
    id: UUID
    run_id: UUID
    thread_id: UUID
    session_id: UUID
    path: str
    name: str
    media_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


class Message(BaseModel):
    id: UUID
    thread_id: UUID
    run_id: UUID | None
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    status: str
    created_at: datetime
    updated_at: datetime


class ThreadDetail(Thread):
    messages: list[Message]
    runs: list[Run]


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
    thread_id: str = Field(alias="threadId")
    run_id: str | None = Field(default=None, alias="runId")
    messages: list[AgUiMessage] = Field(default_factory=list)
    state: Any = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    context: list[dict[str, Any]] = Field(default_factory=list)
    forwarded_props: Any = Field(default=None, alias="forwardedProps")

    model_config = {"populate_by_name": True}
