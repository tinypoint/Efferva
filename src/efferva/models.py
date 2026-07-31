import posixpath
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

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
    last_active_at: datetime
    created_at: datetime
    updated_at: datetime


class ThreadCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    workspace: str | None = Field(default=None, min_length=1, max_length=4096)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    reasoning_effort: Literal[
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "ultra",
    ] | None = None

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("/"):
            raise ValueError("workspace must be an absolute Sandbox path")
        return posixpath.normpath(value)


class RunCreate(BaseModel):
    prompt: str = Field(min_length=1)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    reasoning_effort: Literal[
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "ultra",
    ] | None = None


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
