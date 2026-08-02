from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse

from efferva.api import create_api_router
from efferva.broker import RedisRunBroker
from efferva.codex import CodexGateway
from efferva.codex_appserver import CodexAppServerManager
from efferva.codex_release import prepare_official_codex
from efferva.codex_rpc import CodexRpcClient, CodexRpcError, ServerRequestHandler
from efferva.codex_tunnel import RedisCodexTunnel
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
    UnauthenticatedError,
)
from efferva.repository import (
    NotFoundError,
    RunRepository,
    SessionRepository,
)
from efferva.sandbox import SandboxProvider
from efferva.sandbox.manager import create_sandbox_control_plane


@dataclass(slots=True)
class _RuntimeResources:
    repository: SessionRepository | None = None
    gateway: CodexGateway | None = None
    tunnel: RedisCodexTunnel | None = None
    broker: RedisRunBroker | None = None
    runs: RunRepository | None = None

    def require_repository(self) -> SessionRepository:
        if self.repository is None:
            raise RuntimeError("Efferva application has not started")
        return self.repository

    def require_gateway(self) -> CodexGateway:
        if self.gateway is None:
            raise RuntimeError("Efferva application has not started")
        return self.gateway

    def require_tunnel(self) -> RedisCodexTunnel:
        if self.tunnel is None:
            raise RuntimeError("Efferva application has not started")
        return self.tunnel

    def require_broker(self) -> RedisRunBroker:
        if self.broker is None:
            raise RuntimeError("Efferva application has not started")
        return self.broker

    def require_runs(self) -> RunRepository:
        if self.runs is None:
            raise RuntimeError("Efferva application has not started")
        return self.runs

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
            tunnel = RedisCodexTunnel(
                settings.redis_url,
                prefix=settings.redis_prefix,
                ttl_seconds=settings.redis_run_ttl_seconds,
                lease_seconds=settings.worker_lease_seconds,
                dispatch_queue_capacity=settings.redis_dispatch_queue_capacity,
            )
            sandboxes = None
            await database.open()
            await broker.open()
            await tunnel.open()
            try:
                await database.initialize(schema)
                repository = SessionRepository(
                    database,
                    codex_version=codex_release.version,
                    codex_runtime_sha256=codex_release.binary_sha256,
                )
                runs = RunRepository(database)
                sandboxes = create_sandbox_control_plane(
                    settings,
                    self.sandbox,
                )
                await sandboxes.start()
                app_servers = CodexAppServerManager(
                    codex_release.binary,
                    settings,
                    sandboxes,
                )
                rpc = CodexRpcClient(
                    app_servers,
                    server_request_handler=self.server_request_handler,
                )
                gateway = CodexGateway(
                    settings,
                    rpc,
                    developer_instructions=self.developer_instructions,
                    codex_config=codex_config,
                    native_memory_enabled=self.native_memory_enabled,
                )
                resources.repository = repository
                resources.gateway = gateway
                resources.tunnel = tunnel
                resources.broker = broker
                resources.runs = runs
                yield
            finally:
                resources.runs = None
                resources.broker = None
                resources.tunnel = None
                resources.gateway = None
                resources.repository = None
                try:
                    if sandboxes is not None:
                        await sandboxes.close()
                finally:
                    try:
                        await tunnel.close()
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
                codex_gateway=resources.require_gateway,
                codex_tunnel=resources.require_tunnel,
                run_broker=resources.require_broker,
                runs=resources.require_runs,
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
