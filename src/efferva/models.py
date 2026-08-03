from datetime import datetime
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
    last_active_at: datetime
    created_at: datetime
    updated_at: datetime


class PrincipalView(BaseModel):
    tenant_id: str
    issuer: str
    subject: str
    capabilities: list[Capability]
