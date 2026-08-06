from __future__ import annotations

import asyncio
import json
import posixpath
import secrets
import shlex
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse

from efferva.api import principal_dependency
from efferva.db import Database
from efferva.identity import IdentityResolver, Principal
from efferva.sandbox.protocol import SandboxEnvironment
from efferva.sandbox.service import SessionSandboxService
from efferva.session_repository import AccessMode, SessionRepository

_SDK_VERSION = "0.2.131"
_CLI_VERSION = "2.1.223"
_SERVER_PORT = 4500
_RUNTIME_ROOT = f"/opt/efferva/runtimes/claude-code/{_SDK_VERSION}"


@dataclass(frozen=True, slots=True)
class ClaudeCode:
    api_key: str
    base_url: str | None = None
    model: str | None = None

    name = "claude-code"
    protocol = "claude-agent-sdk-sse"

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError("ClaudeCode api_key is required")

    def create_router(
        self,
        *,
        identity: IdentityResolver,
        repository: SessionRepository,
        sandboxes: SessionSandboxService,
        database: Database,
    ) -> APIRouter:
        manager = ClaudeCodeServerManager(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            sandboxes=sandboxes,
            database=database,
        )
        resolve_principal = principal_dependency(identity)
        router = APIRouter()

        async def target(
            principal: Principal,
            session_id: UUID,
            mode: AccessMode,
            *,
            touch: bool = False,
        ) -> tuple[str, dict[str, str]]:
            session = await repository.get_session(
                principal,
                session_id,
                mode=mode,
                touch=touch,
            )
            return await manager.connection_target(session)

        @router.get("/api/sessions/{session_id}/claude/threads")
        async def list_threads(
            session_id: UUID,
            principal: Principal = Depends(resolve_principal),
        ) -> Response:
            base_url, headers = await target(principal, session_id, AccessMode.READ)
            return await _json_proxy("GET", f"{base_url}/threads", headers)

        @router.get("/api/sessions/{session_id}/claude/threads/{thread_id}")
        async def get_thread(
            session_id: UUID,
            thread_id: str,
            principal: Principal = Depends(resolve_principal),
        ) -> Response:
            base_url, headers = await target(principal, session_id, AccessMode.READ)
            return await _json_proxy(
                "GET",
                f"{base_url}/threads/{thread_id}",
                headers,
            )

        @router.delete("/api/sessions/{session_id}/claude/threads/{thread_id}")
        async def delete_thread(
            session_id: UUID,
            thread_id: str,
            principal: Principal = Depends(resolve_principal),
        ) -> Response:
            base_url, headers = await target(principal, session_id, AccessMode.WRITE)
            return await _json_proxy(
                "DELETE",
                f"{base_url}/threads/{thread_id}",
                headers,
            )

        @router.post("/api/sessions/{session_id}/claude/messages")
        async def post_message(
            session_id: UUID,
            request: Request,
            principal: Principal = Depends(resolve_principal),
        ) -> Response:
            base_url, headers = await target(
                principal,
                session_id,
                AccessMode.WRITE,
                touch=True,
            )
            payload = await request.body()
            return await _sse_proxy(
                f"{base_url}/messages",
                headers,
                payload,
            )

        return router


class ClaudeCodeServerManager:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None,
        model: str | None,
        sandboxes: SessionSandboxService,
        database: Database,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._sandboxes = sandboxes
        self._database = database
        self._server_source = (
            files("efferva").joinpath("engines/claude_server.py").read_bytes()
        )

    async def connection_target(
        self,
        session: Mapping[str, Any],
    ) -> tuple[str, dict[str, str]]:
        sandbox = await self._ensure(session)
        endpoint, endpoint_headers = await sandbox.runtime.get_endpoint(_SERVER_PORT)
        token = (
            (await sandbox.runtime.read_file(self._token_file(sandbox)))
            .decode()
            .strip()
        )
        return _http_url(endpoint), {
            **endpoint_headers,
            "Authorization": f"Bearer {token}",
        }

    async def _ensure(self, session: Mapping[str, Any]) -> SandboxEnvironment:
        sandbox = await self._sandboxes.ensure(session)
        session_id = UUID(str(session["id"]))
        effective_key = (
            "efferva-credential-proxy"
            if sandbox.sandbox.state.get("credentialProxy")
            else self._api_key
        )
        launch_hash = self._launch_hash(sandbox, effective_key)
        async with self._database.advisory_lock(
            f"efferva:claude-code-server-session:{session_id}"
        ):
            if not await self._is_running(sandbox, launch_hash):
                await self._install_and_start(sandbox, effective_key, launch_hash)
        await self._wait_until_ready(sandbox)
        return sandbox

    def _launch_hash(self, sandbox: SandboxEnvironment, effective_key: str) -> str:
        identity = sandbox.layout.identity
        return sha256(
            json.dumps(
                {
                    "sdkVersion": _SDK_VERSION,
                    "cliVersion": _CLI_VERSION,
                    "serverSha256": sha256(self._server_source).hexdigest(),
                    "apiKeySha256": sha256(effective_key.encode()).hexdigest(),
                    "baseUrl": self._base_url,
                    "model": self._model,
                    "uid": identity.uid,
                    "gid": identity.gid,
                    "home": identity.home_path,
                    "workspace": sandbox.layout.workspace_path,
                },
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    async def _is_running(
        self,
        sandbox: SandboxEnvironment,
        launch_hash: str,
    ) -> bool:
        try:
            record = (
                (await sandbox.runtime.read_file(self._pid_file())).decode().split()
            )
            if len(record) != 2 or record[1] != launch_hash:
                return False
            base_url, headers = await self._raw_connection_target(sandbox)
            async with httpx.AsyncClient(timeout=1) as client:
                response = await client.get(f"{base_url}/health", headers=headers)
            return response.is_success
        except Exception:
            return False

    async def _install_and_start(
        self,
        sandbox: SandboxEnvironment,
        effective_key: str,
        launch_hash: str,
    ) -> None:
        await self._validate_runtime(sandbox)
        identity = sandbox.layout.identity
        workspace = sandbox.layout.workspace_path
        server_file = posixpath.join(_RUNTIME_ROOT, "server.py")
        venv_python = posixpath.join(_RUNTIME_ROOT, "venv/bin/python")
        marker = posixpath.join(_RUNTIME_ROOT, ".installed")
        try:
            await sandbox.runtime.stat(marker)
        except FileNotFoundError:
            command = (
                f"mkdir -p {shlex.quote(_RUNTIME_ROOT)} && "
                f"python3 -m venv {shlex.quote(posixpath.join(_RUNTIME_ROOT, 'venv'))} && "
                f"{shlex.quote(venv_python)} -m pip install --no-cache-dir "
                f"claude-agent-sdk=={_SDK_VERSION} "
                "fastapi==0.116.1 uvicorn==0.35.0 && "
                f"printf '%s\n' {_SDK_VERSION!r} >{shlex.quote(marker)}"
            )
            await _run(sandbox, ("sh", "-lc", command))
        await sandbox.runtime.write_file(server_file, self._server_source)

        home = identity.home_path
        state_dir = posixpath.join(home, ".efferva/claude-code")
        token_file = self._token_file(sandbox)
        log_file = posixpath.join(state_dir, "server.log")
        token = secrets.token_urlsafe(32)
        await _run(
            sandbox,
            (
                "sh",
                "-lc",
                f"mkdir -p {shlex.quote(home)} {shlex.quote(workspace)} "
                f"{shlex.quote(state_dir)} && "
                f"chown -R {identity.uid}:{identity.gid} {shlex.quote(home)} && "
                f"chmod 755 {shlex.quote(server_file)}",
            ),
        )
        await _run(
            sandbox,
            (
                "sh",
                "-lc",
                f"if [ ! -s {shlex.quote(token_file)} ]; then "
                f"umask 077; printf '%s\\n' {shlex.quote(token)} >{shlex.quote(token_file)}; "
                "fi; "
                f"touch {shlex.quote(log_file)}; chmod 600 "
                f"{shlex.quote(token_file)} {shlex.quote(log_file)}",
            ),
            uid=identity.uid,
            gid=identity.gid,
        )
        actual_token = (await sandbox.runtime.read_file(token_file)).decode().strip()
        pid_file = self._pid_file()
        await _run(
            sandbox,
            (
                "sh",
                "-lc",
                f"if [ -s {shlex.quote(pid_file)} ]; then "
                f"read -r pid old_hash <{shlex.quote(pid_file)}; "
                'kill "$pid" 2>/dev/null || true; '
                f"rm -f {shlex.quote(pid_file)}; fi",
            ),
        )
        environment = {
            "HOME": home,
            "CLAUDE_CONFIG_DIR": posixpath.join(home, ".claude"),
            "ANTHROPIC_API_KEY": effective_key,
            "EFFERVA_CLAUDE_WORKSPACE": workspace,
            "EFFERVA_CLAUDE_SERVER_TOKEN": actual_token,
        }
        if self._base_url:
            environment["ANTHROPIC_BASE_URL"] = self._base_url
        if self._model:
            environment["EFFERVA_CLAUDE_MODEL"] = self._model
        command = (
            f"cd {shlex.quote(workspace)} && "
            f"{shlex.quote(venv_python)} -m uvicorn server:app "
            f"--app-dir {shlex.quote(_RUNTIME_ROOT)} --host 0.0.0.0 --port {_SERVER_PORT} "
            f"</dev/null >>{shlex.quote(log_file)} 2>&1 & "
            f'echo "$! {launch_hash}" >{shlex.quote(pid_file)}'
        )
        await _run(
            sandbox,
            ("sh", "-lc", command),
            env=environment,
            uid=identity.uid,
            gid=identity.gid,
        )

    async def _validate_runtime(self, sandbox: SandboxEnvironment) -> None:
        script = (
            "import ctypes,platform,sys;"
            "a=platform.machine().lower();"
            "assert sys.platform.startswith('linux'), 'Claude Code requires Linux';"
            "assert a in {'x86_64','amd64','aarch64','arm64'}, f'unsupported architecture: {a}';"
            "assert sys.version_info >= (3,11), 'Claude Code requires Python 3.11+';"
            "v=platform.libc_ver();"
            "assert v[0]=='glibc', 'Claude Code does not support musl/Alpine';"
            "parts=tuple(map(int,v[1].split('.')[:2]));"
            "assert parts >= (2,17), f'Claude Code requires glibc 2.17+, found {v[1]}'"
        )
        result = await sandbox.runtime.run_command(
            ("python3", "-c", script),
            cwd="/",
        )
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout).decode(errors="replace")
            raise RuntimeError(
                detail or "Sandbox does not satisfy Claude Code runtime requirements"
            )
        pip = await sandbox.runtime.run_command(
            ("python3", "-m", "pip", "--version"),
            cwd="/",
        )
        if pip.exit_code != 0:
            raise RuntimeError("Claude Code requires Python 3.11+ with pip and venv")

    async def _wait_until_ready(self, sandbox: SandboxEnvironment) -> None:
        last_error: Exception | None = None
        for attempt in range(8):
            try:
                base_url, headers = await self._raw_connection_target(sandbox)
                async with httpx.AsyncClient(timeout=2) as client:
                    response = await client.get(f"{base_url}/health", headers=headers)
                    response.raise_for_status()
                return
            except Exception as error:
                last_error = error
                await asyncio.sleep(0.1 * (2**attempt))
        raise RuntimeError(f"Claude Code Server did not start: {last_error}")

    async def _raw_connection_target(
        self,
        sandbox: SandboxEnvironment,
    ) -> tuple[str, dict[str, str]]:
        endpoint, headers = await sandbox.runtime.get_endpoint(_SERVER_PORT)
        token = (
            (await sandbox.runtime.read_file(self._token_file(sandbox)))
            .decode()
            .strip()
        )
        return _http_url(endpoint), {
            **headers,
            "Authorization": f"Bearer {token}",
        }

    @staticmethod
    def _token_file(sandbox: SandboxEnvironment) -> str:
        return posixpath.join(
            sandbox.layout.identity.home_path,
            ".efferva/claude-code/server.token",
        )

    @staticmethod
    def _pid_file() -> str:
        return "/tmp/efferva-claude-code-server.pid"


async def _json_proxy(
    method: str,
    url: str,
    headers: Mapping[str, str],
) -> Response:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.request(method, url, headers=headers)
    content_type = response.headers.get("content-type", "application/json")
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=content_type.split(";", 1)[0],
    )


async def _sse_proxy(
    url: str,
    headers: Mapping[str, str],
    payload: bytes,
) -> Response:
    client = httpx.AsyncClient(timeout=None)
    upstream = await client.send(
        client.build_request(
            "POST",
            url,
            headers={**headers, "Content-Type": "application/json"},
            content=payload,
        ),
        stream=True,
    )
    if upstream.status_code != 200:
        content = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        return Response(
            content=content,
            status_code=upstream.status_code,
            media_type="application/json",
        )

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _run(
    sandbox: SandboxEnvironment,
    argv: tuple[str, ...],
    *,
    env: Mapping[str, str] | None = None,
    uid: int | None = None,
    gid: int | None = None,
) -> None:
    result = await sandbox.runtime.run_command(
        argv,
        cwd="/",
        env=env,
        uid=uid,
        gid=gid,
    )
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout).decode(errors="replace")
        raise RuntimeError(detail or f"sandbox command exited {result.exit_code}")


def _http_url(endpoint: str) -> str:
    endpoint = endpoint.rstrip("/")
    if endpoint.startswith(("http://", "https://")):
        return endpoint
    return f"http://{endpoint}"
