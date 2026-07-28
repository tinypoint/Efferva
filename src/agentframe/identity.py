from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from starlette.requests import Request

LEGACY_TENANT_ID = "__agentframe_legacy__"
LEGACY_ISSUER = "agentframe:legacy"


class Capability(StrEnum):
    """Framework capabilities supplied by the product identity adapter."""

    SESSIONS_READ_TENANT = "sessions:read:tenant"
    SESSIONS_WRITE_TENANT = "sessions:write:tenant"


@dataclass(frozen=True, slots=True)
class Principal:
    """A product-authenticated actor within one active tenant."""

    tenant_id: str
    issuer: str
    subject: str
    capabilities: frozenset[Capability] = frozenset()

    def __post_init__(self) -> None:
        for field_name in ("tenant_id", "issuer", "subject"):
            value = getattr(self, field_name).strip()
            if not value:
                raise ValueError(f"{field_name} must not be empty")
            if len(value) > 255:
                raise ValueError(f"{field_name} must not exceed 255 characters")
            object.__setattr__(self, field_name, value)

        if self.tenant_id == LEGACY_TENANT_ID or self.issuer == LEGACY_ISSUER:
            raise ValueError("reserved AgentFrame legacy identity")

        capabilities = frozenset(Capability(value) for value in self.capabilities)
        object.__setattr__(self, "capabilities", capabilities)

    def has(self, capability: Capability) -> bool:
        return capability in self.capabilities


class IdentityResolver(Protocol):
    def __call__(self, request: Request) -> Awaitable[Principal]: ...


class UnauthenticatedError(PermissionError):
    """Raised by an identity resolver when no authenticated product user exists."""


class ForbiddenError(PermissionError):
    """Raised when an authenticated principal requests a broader scope than allowed."""
