from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.resources import files

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse

from efferva.api import create_api_router
from efferva.codex_release import prepare_official_codex
from efferva.codex_rpc import CodexRpcError
from efferva.codex_tunnel import RedisCodexTunnel
from efferva.config import Settings, get_settings
from efferva.db import Database
from efferva.identity import (
    ForbiddenError,
    IdentityResolver,
    UnauthenticatedError,
)
from efferva.repository import (
    NotFoundError,
    SessionRepository,
)


@dataclass(slots=True)
class _RuntimeResources:
    repository: SessionRepository | None = None
    tunnel: RedisCodexTunnel | None = None

    def require_repository(self) -> SessionRepository:
        if self.repository is None:
            raise RuntimeError("Efferva application has not started")
        return self.repository

    def require_tunnel(self) -> RedisCodexTunnel:
        if self.tunnel is None:
            raise RuntimeError("Efferva application has not started")
        return self.tunnel

class Efferva:
    """Product-facing facade for installing Efferva into an authenticated application."""

    def __init__(
        self,
        *,
        identity: IdentityResolver,
        settings: Settings | None = None,
    ) -> None:
        self.identity = identity
        self.settings = settings or get_settings()

    def install(self, app: FastAPI, *, prefix: str = "/agent") -> None:
        normalized_prefix = self._normalize_prefix(prefix)
        installed_prefixes = getattr(app.state, "_efferva_prefixes", set())
        if normalized_prefix in installed_prefixes:
            raise RuntimeError(f"Efferva is already installed at {normalized_prefix or '/'}")
        app.state._efferva_prefixes = {*installed_prefixes, normalized_prefix}

        resources = _RuntimeResources()
        settings = self.settings
        schema = files("efferva").joinpath("schema.sql").read_text()

        @asynccontextmanager
        async def lifespan(_: FastAPI) -> AsyncIterator[None]:
            codex_release = await prepare_official_codex(settings)
            database = Database(settings.database_url)
            tunnel = RedisCodexTunnel(
                settings.redis_url,
                prefix=settings.redis_prefix,
                ttl_seconds=settings.redis_run_ttl_seconds,
                lease_seconds=settings.worker_lease_seconds,
                dispatch_queue_capacity=settings.redis_dispatch_queue_capacity,
            )
            await database.open()
            await tunnel.open()
            try:
                await database.initialize(schema)
                repository = SessionRepository(
                    database,
                    codex_version=codex_release.version,
                    codex_runtime_sha256=codex_release.binary_sha256,
                )
                resources.repository = repository
                resources.tunnel = tunnel
                yield
            finally:
                resources.tunnel = None
                resources.repository = None
                try:
                    await tunnel.close()
                finally:
                    await database.close()

        router = APIRouter(lifespan=lifespan)
        router.include_router(
            create_api_router(
                identity=self.identity,
                repository=resources.require_repository,
                codex_tunnel=resources.require_tunnel,
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

        async def unauthenticated_handler(_, error: UnauthenticatedError) -> JSONResponse:
            detail = str(error) or "authentication required"
            return JSONResponse(status_code=401, content={"detail": detail})

        async def forbidden_handler(_, error: ForbiddenError) -> JSONResponse:
            return JSONResponse(status_code=403, content={"detail": str(error)})

        async def codex_rpc_handler(_, error: CodexRpcError) -> JSONResponse:
            return JSONResponse(
                status_code=502,
                content={"detail": str(error), "codex_error": error.error},
            )

        app.add_exception_handler(NotFoundError, not_found_handler)
        app.add_exception_handler(UnauthenticatedError, unauthenticated_handler)
        app.add_exception_handler(ForbiddenError, forbidden_handler)
        app.add_exception_handler(CodexRpcError, codex_rpc_handler)
