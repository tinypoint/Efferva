from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse

from efferva.api import create_api_router
from efferva.codex_appserver import CodexAppServerManager
from efferva.config import Settings, get_settings
from efferva.db import Database
from efferva.identity import (
    ForbiddenError,
    IdentityResolver,
    UnauthenticatedError,
)
from efferva.session_repository import (
    NotFoundError,
    SessionRepository,
)
from efferva.sandbox import SandboxProvider
from efferva.sandbox.service import SessionSandboxService


class Efferva:
    """Product-facing facade for installing Efferva into an authenticated application."""

    def __init__(
        self,
        *,
        identity: IdentityResolver,
        sandbox: SandboxProvider,
        settings: Settings | None = None,
    ) -> None:
        self.identity = identity
        self.sandbox = sandbox
        self.settings = settings or get_settings()

    def install(self, app: FastAPI, *, prefix: str = "/agent") -> None:
        normalized_prefix = self._normalize_prefix(prefix)
        installed_prefixes = getattr(app.state, "_efferva_prefixes", set())
        if normalized_prefix in installed_prefixes:
            raise RuntimeError(
                f"Efferva is already installed at {normalized_prefix or '/'}"
            )
        app.state._efferva_prefixes = {*installed_prefixes, normalized_prefix}

        settings = self.settings
        if settings.database_url is None:
            raise ValueError("EFFERVA_DATABASE_URL is required by the API")
        schema = files("efferva").joinpath("schema.sql").read_text()
        database = Database(settings.database_url)
        sandboxes = SessionSandboxService(settings, self.sandbox, database)
        repository = SessionRepository(database)
        codex = CodexAppServerManager(settings, sandboxes, database)

        @asynccontextmanager
        async def lifespan(_: FastAPI) -> AsyncIterator[None]:
            await database.open()
            try:
                await sandboxes.open()
                try:
                    await database.initialize(schema)
                    yield
                finally:
                    await sandboxes.close()
            finally:
                await database.close()

        router = APIRouter(lifespan=lifespan)
        router.include_router(
            create_api_router(
                identity=self.identity,
                repository=repository,
                codex=codex,
            )
        )

        app.include_router(router, prefix=normalized_prefix)
        self._install_exception_handlers(app)

    def asgi_app(self) -> FastAPI:
        app = FastAPI(title="Efferva")
        self.install(app, prefix="")
        return app

    def app(self) -> FastAPI:
        """Compatibility alias for asgi_app()."""

        return self.asgi_app()

    @staticmethod
    def _normalize_prefix(prefix: str) -> str:
        if prefix in {"", "/"}:
            return ""
        if not prefix.startswith("/"):
            raise ValueError("prefix must start with '/'")
        return prefix.rstrip("/")

    @staticmethod
    def _install_exception_handlers(app: FastAPI) -> None:
        async def not_found_handler(_, error: NotFoundError) -> JSONResponse:
            return JSONResponse(status_code=404, content={"detail": str(error)})

        async def unauthenticated_handler(
            _, error: UnauthenticatedError
        ) -> JSONResponse:
            detail = str(error) or "authentication required"
            return JSONResponse(status_code=401, content={"detail": detail})

        async def forbidden_handler(_, error: ForbiddenError) -> JSONResponse:
            return JSONResponse(status_code=403, content={"detail": str(error)})

        app.add_exception_handler(NotFoundError, not_found_handler)
        app.add_exception_handler(UnauthenticatedError, unauthenticated_handler)
        app.add_exception_handler(ForbiddenError, forbidden_handler)
