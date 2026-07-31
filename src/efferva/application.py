from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.resources import files
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from efferva.api import create_api_router, principal_dependency
from efferva.capabilities import SkillRoot
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
    ConflictError,
    NotFoundError,
    SystemRepository,
)
from efferva.runtime import CodexProxy, CodexRpcError
from efferva.sandbox import create_sandbox_control_plane, register_sandbox_provider


@dataclass(slots=True)
class _RuntimeResources:
    repository: SystemRepository | None = None
    proxy: CodexProxy | None = None

    def require_repository(self) -> SystemRepository:
        if self.repository is None:
            raise RuntimeError("Efferva application has not started")
        return self.repository

    def require_proxy(self) -> CodexProxy:
        if self.proxy is None:
            raise RuntimeError("Efferva application has not started")
        return self.proxy

class Efferva:
    """Product-facing facade for installing Efferva into an authenticated application."""

    def __init__(
        self,
        *,
        identity: IdentityResolver,
        settings: Settings | None = None,
        codex_config: Mapping[str, Any] | None = None,
        developer_instructions: str | None = None,
        skill_roots: list[SkillRoot] | tuple[SkillRoot, ...] | None = None,
        native_memory_enabled: bool = True,
    ) -> None:
        self.identity = identity
        self.settings = settings or get_settings()
        self.codex_config = dict(codex_config or {})
        self.developer_instructions = developer_instructions
        self.skill_roots = tuple(skill_roots or ())
        self.native_memory_enabled = native_memory_enabled
        skill_root_ids = [root.id for root in self.skill_roots]
        if len(skill_root_ids) != len(set(skill_root_ids)):
            raise ValueError("Efferva SkillRoot ids must be unique")

    def install(self, app: FastAPI, *, prefix: str = "/agent") -> None:
        normalized_prefix = self._normalize_prefix(prefix)
        installed_prefixes = getattr(app.state, "_efferva_prefixes", set())
        if normalized_prefix in installed_prefixes:
            raise RuntimeError(f"Efferva is already installed at {normalized_prefix or '/'}")
        app.state._efferva_prefixes = {*installed_prefixes, normalized_prefix}

        resources = _RuntimeResources()
        settings = self.settings
        migrations_dir = files("efferva.migrations")
        migrations = [
            (path.name, path.read_text())
            for path in sorted(migrations_dir.iterdir(), key=lambda item: item.name)
            if path.name.endswith(".sql")
        ]

        @asynccontextmanager
        async def lifespan(_: FastAPI) -> AsyncIterator[None]:
            codex_release = await prepare_official_codex(settings)
            codex_config = merge_codex_config(
                load_codex_config(settings.codex_config_file),
                self.codex_config,
            )
            database = Database(settings.database_url)
            sandboxes = None
            await database.open()
            try:
                await database.migrate(migrations)
                repository = SystemRepository(
                    database,
                    codex_version=codex_release.version,
                    codex_runtime_sha256=codex_release.binary_sha256,
                )
                sandboxes = create_sandbox_control_plane(settings)
                await sandboxes.start()
                proxy = CodexProxy(
                    codex_release.binary,
                    settings,
                    sandboxes,
                    developer_instructions=self.developer_instructions,
                    codex_config=codex_config,
                    skill_roots=self.skill_roots,
                    native_memory_enabled=self.native_memory_enabled,
                )
                resources.repository = repository
                resources.proxy = proxy
                yield
            finally:
                resources.proxy = None
                resources.repository = None
                try:
                    if sandboxes is not None:
                        await sandboxes.close()
                finally:
                    await database.close()

        router = APIRouter(lifespan=lifespan)
        router.include_router(
            create_api_router(
                identity=self.identity,
                system_repository=resources.require_repository,
                codex_proxy=resources.require_proxy,
            )
        )

        static_dir = files("efferva").joinpath("static")
        require_principal = principal_dependency(self.identity)
        IndexPrincipal = Annotated[Principal, Depends(require_principal)]

        @router.get("/", include_in_schema=False)
        async def index(_: IndexPrincipal) -> FileResponse:
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

    @classmethod
    def register_sandbox_provider(cls, name: str, provider) -> None:
        register_sandbox_provider(name, provider)

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

        async def conflict_handler(_, error: ConflictError) -> JSONResponse:
            return JSONResponse(status_code=409, content={"detail": str(error)})

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
        app.add_exception_handler(ConflictError, conflict_handler)
        app.add_exception_handler(UnauthenticatedError, unauthenticated_handler)
        app.add_exception_handler(ForbiddenError, forbidden_handler)
        app.add_exception_handler(CodexRpcError, codex_rpc_handler)
