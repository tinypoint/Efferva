from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from efferva.api import create_api_router, principal_dependency
from efferva.broker import RedisRunBroker
from efferva.codex_release import prepare_official_codex
from efferva.config import (
    Settings,
    get_settings,
    load_codex_config,
    merge_codex_config,
)
from efferva.db import Database
from efferva.identity import (
    ForbiddenError,
    IdentityResolver,
    Principal,
    UnauthenticatedError,
)
from efferva.repository import (
    NotFoundError,
    RunRepository,
    SessionDefaultsRepository,
    SessionRepository,
)
from efferva.runtime import CodexProxy, CodexRpcError, ServerRequestHandler
from efferva.sandbox import SandboxProvider, create_sandbox_control_plane


@dataclass(slots=True)
class _RuntimeResources:
    repository: SessionRepository | None = None
    proxy: CodexProxy | None = None
    broker: RedisRunBroker | None = None
    runs: RunRepository | None = None
    session_defaults: SessionDefaultsRepository | None = None

    def require_repository(self) -> SessionRepository:
        if self.repository is None:
            raise RuntimeError("Efferva application has not started")
        return self.repository

    def require_proxy(self) -> CodexProxy:
        if self.proxy is None:
            raise RuntimeError("Efferva application has not started")
        return self.proxy

    def require_broker(self) -> RedisRunBroker:
        if self.broker is None:
            raise RuntimeError("Efferva application has not started")
        return self.broker

    def require_runs(self) -> RunRepository:
        if self.runs is None:
            raise RuntimeError("Efferva application has not started")
        return self.runs

    def require_session_defaults(self) -> SessionDefaultsRepository:
        if self.session_defaults is None:
            raise RuntimeError("Efferva application has not started")
        return self.session_defaults


class Efferva:
    """Product-facing facade for installing Efferva into an authenticated application."""

    def __init__(
        self,
        *,
        identity: IdentityResolver,
        sandbox: SandboxProvider,
        settings: Settings | None = None,
        codex_config: Mapping[str, Any] | None = None,
        developer_instructions: str | None = None,
        native_memory_enabled: bool = True,
        server_request_handler: ServerRequestHandler | None = None,
    ) -> None:
        self.identity = identity
        self.sandbox = sandbox
        self.settings = settings or get_settings()
        self.codex_config = dict(codex_config or {})
        self.developer_instructions = developer_instructions
        self.native_memory_enabled = native_memory_enabled
        self.server_request_handler = server_request_handler

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
            codex_config = merge_codex_config(
                load_codex_config(settings.codex_config_file),
                self.codex_config,
            )
            database = Database(settings.database_url)
            broker = RedisRunBroker(
                settings.redis_url,
                prefix=settings.redis_prefix,
                event_ttl_seconds=settings.redis_run_ttl_seconds,
                event_stream_maxlen=settings.redis_event_stream_maxlen,
                command_stream_maxlen=settings.redis_command_stream_maxlen,
                dispatch_queue_capacity=settings.redis_dispatch_queue_capacity,
                event_max_bytes=settings.redis_event_max_bytes,
                command_max_bytes=settings.redis_command_max_bytes,
            )
            sandboxes = None
            await database.open()
            await broker.open()
            try:
                await database.initialize(schema)
                repository = SessionRepository(
                    database,
                    codex_version=codex_release.version,
                    codex_runtime_sha256=codex_release.binary_sha256,
                )
                runs = RunRepository(database)
                session_defaults = SessionDefaultsRepository(database)
                sandboxes = create_sandbox_control_plane(
                    settings,
                    self.sandbox,
                )
                await sandboxes.start()
                proxy = CodexProxy(
                    codex_release.binary,
                    settings,
                    sandboxes,
                    developer_instructions=self.developer_instructions,
                    codex_config=codex_config,
                    native_memory_enabled=self.native_memory_enabled,
                    server_request_handler=self.server_request_handler,
                )
                resources.repository = repository
                resources.proxy = proxy
                resources.broker = broker
                resources.runs = runs
                resources.session_defaults = session_defaults
                yield
            finally:
                resources.session_defaults = None
                resources.runs = None
                resources.broker = None
                resources.proxy = None
                resources.repository = None
                try:
                    if sandboxes is not None:
                        await sandboxes.close()
                finally:
                    try:
                        await broker.close()
                    finally:
                        await database.close()

        router = APIRouter(lifespan=lifespan)
        router.include_router(
            create_api_router(
                identity=self.identity,
                repository=resources.require_repository,
                codex_proxy=resources.require_proxy,
                run_broker=resources.require_broker,
                runs=resources.require_runs,
                session_defaults=resources.require_session_defaults,
            )
        )

        static_dir = files("efferva").joinpath("static")
        require_principal = principal_dependency(self.identity)

        @router.get("/", include_in_schema=False)
        async def index(
            _: Principal = Depends(require_principal),
        ) -> FileResponse:
            return FileResponse(str(static_dir.joinpath("index.html")))

        @router.get("/static/{asset_name}", include_in_schema=False)
        async def static_asset(asset_name: str) -> FileResponse:
            if asset_name not in {"app.js", "style.css"}:
                raise HTTPException(status_code=404, detail="asset not found")
            return FileResponse(str(static_dir.joinpath(asset_name)))

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
