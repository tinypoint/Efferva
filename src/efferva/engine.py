from typing import Protocol

from fastapi import APIRouter

from efferva.db import Database
from efferva.identity import IdentityResolver
from efferva.sandbox.service import SessionSandboxService
from efferva.session_repository import SessionRepository


class Engine(Protocol):
    """The smallest interface shared by Efferva's real agent engines."""

    name: str
    protocol: str

    def create_router(
        self,
        *,
        identity: IdentityResolver,
        repository: SessionRepository,
        sandboxes: SessionSandboxService,
        database: Database,
    ) -> APIRouter: ...
