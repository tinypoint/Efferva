from __future__ import annotations

import asyncio
import json
import os
import posixpath
import secrets
import shlex
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

from efferva.config import Settings
from efferva.sandbox.manager import SandboxControlPlane
from efferva.sandbox.protocol import SandboxEnvironment


class CodexAppServerManager:
    """Installs and starts one Codex app-server per Efferva Session."""

    def __init__(
        self,
        binary: Path,
        settings: Settings,
        sandboxes: SandboxControlPlane,
    ) -> None:
        self._binary_bytes = binary.read_bytes()
        self._binary_sha256 = sha256(self._binary_bytes).hexdigest()
        self._settings = settings
        self._sandboxes = sandboxes
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._sessions: dict[UUID, SandboxEnvironment] = {}
        self._connection_targets: dict[str, tuple[str, dict[str, str]]] = {}

    async def connection_target(
        self,
        session: Mapping[str, Any],
    ) -> tuple[str, dict[str, str]]:
        sandbox = await self._ensure(session)
        cached = self._connection_targets.get(sandbox.environment_id)
        if cached is not None:
            return cached
        endpoint, headers = await sandbox.runtime.get_endpoint(
            self._settings.codex_appserver_port
        )
        websocket_token = (
            await sandbox.runtime.read_file(
                posixpath.join(
                    self._settings.codex_home_path,
                    "app-server.token",
                )
            )
        ).decode("utf-8").strip()
        target = (
            _websocket_url(endpoint),
            {
                **headers,
                "Authorization": f"Bearer {websocket_token}",
            },
        )
        self._connection_targets[sandbox.environment_id] = target
        return target

    async def _ensure(
        self,
        session: Mapping[str, Any],
    ) -> SandboxEnvironment:
        session_id = UUID(str(session["id"]))
        cached = self._sessions.get(session_id)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            cached = self._sessions.get(session_id)
            if cached is not None:
                return cached
            sandbox = await self._sandboxes.ensure(session_id)
            await self._install_and_start(sandbox)
            self._sessions[session_id] = sandbox
            return sandbox

    async def _install_and_start(self, sandbox: SandboxEnvironment) -> None:
        runtime_root = posixpath.join(
            self._settings.codex_runtime_dir,
            self._binary_sha256,
        )
        sandbox_binary = posixpath.join(runtime_root, "codex")
        try:
            await sandbox.runtime.stat(sandbox_binary)
        except FileNotFoundError:
            await self._run_command(
                sandbox,
                (
                    "sh",
                    "-lc",
                    f"mkdir -p {shlex.quote(runtime_root)}",
                ),
            )
            await sandbox.runtime.write_file(sandbox_binary, self._binary_bytes)

        codex_home = self._settings.codex_home_path
        websocket_token_file = posixpath.join(codex_home, "app-server.token")
        websocket_token = secrets.token_urlsafe(32)
        temporary_token_file = (
            f"{websocket_token_file}.{websocket_token[:16]}.tmp"
        )
        pid_file = "/tmp/efferva-app-server.pid"
        log_file = posixpath.join(codex_home, "app-server.log")
        start_lock = "/tmp/efferva-app-server-start.lock"
        listen = f"ws://0.0.0.0:{self._settings.codex_appserver_port}"
        app_server_overrides: dict[str, str] = {}
        if self._settings.codex_openai_base_url:
            app_server_overrides = {
                "model_providers.efferva_proxy.name": "Efferva LLM proxy",
                "model_providers.efferva_proxy.base_url": (
                    self._settings.codex_openai_base_url
                ),
                "model_providers.efferva_proxy.env_key": "OPENAI_API_KEY",
                "model_providers.efferva_proxy.wire_api": "responses",
                "model_provider": "efferva_proxy",
            }
        app_server_config_args = tuple(
            f"{key}={json.dumps(value)}"
            for key, value in app_server_overrides.items()
        )
        api_key = os.environ.get("OPENAI_API_KEY")
        effective_api_key = None
        if api_key:
            effective_api_key = (
                "efferva-credential-proxy"
                if sandbox.sandbox.state.get("credentialProxy")
                else api_key
            )
        app_server_launch_sha256 = sha256(
            json.dumps(
                {
                    "binary": self._binary_sha256,
                    "config": app_server_config_args,
                    "openaiApiKeySha256": (
                        sha256(effective_api_key.encode()).hexdigest()
                        if effective_api_key
                        else None
                    ),
                },
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        bootstrap = (
            f"if [ -d {shlex.quote(posixpath.join(self._settings.session_volume_path, 'codex-home'))} ] "
            f"&& [ ! -e {shlex.quote(codex_home)} ]; then "
            f"mv {shlex.quote(posixpath.join(self._settings.session_volume_path, 'codex-home'))} "
            f"{shlex.quote(codex_home)}; fi; "
            "if ! id sandbox >/dev/null 2>&1; then "
            "if command -v useradd >/dev/null 2>&1; then "
            f"useradd -u {self._settings.sandbox_uid} "
            f"-d {shlex.quote(self._settings.session_volume_path)} "
            "-M -s /bin/sh sandbox 2>/dev/null || true; "
            "elif command -v adduser >/dev/null 2>&1; then "
            f"adduser -D -u {self._settings.sandbox_uid} "
            f"-h {shlex.quote(self._settings.session_volume_path)} "
            "sandbox 2>/dev/null || true; fi; fi; "
            f"mkdir -p {shlex.quote(codex_home)} "
            f"{shlex.quote(self._settings.workspace_path)} && "
            f"if [ ! -s {shlex.quote(websocket_token_file)} ]; then "
            f"printf '%s\\n' {shlex.quote(websocket_token)} "
            f">{shlex.quote(temporary_token_file)} && "
            f"chmod 600 {shlex.quote(temporary_token_file)} && "
            f"ln {shlex.quote(temporary_token_file)} "
            f"{shlex.quote(websocket_token_file)} 2>/dev/null || true; "
            f"rm -f {shlex.quote(temporary_token_file)}; fi && "
            f"chmod 755 {shlex.quote(sandbox_binary)} && "
            f"chown {self._settings.sandbox_uid}:{self._settings.sandbox_gid} "
            f"{shlex.quote(self._settings.session_volume_path)} "
            f"{shlex.quote(codex_home)} "
            f"{shlex.quote(self._settings.workspace_path)}"
        )
        await self._run_command(sandbox, ("sh", "-lc", bootstrap))
        app_server_cli_config = "".join(
            f"-c {shlex.quote(argument)} "
            for argument in app_server_config_args
        )
        command = (
            f"if ! mkdir {shlex.quote(start_lock)} 2>/dev/null; then exit 0; fi; "
            f"trap 'rmdir {shlex.quote(start_lock)} 2>/dev/null || true' EXIT; "
            f"if [ -s {shlex.quote(pid_file)} ]; then "
            f"read -r efferva_pid efferva_sha <{shlex.quote(pid_file)}; "
            f"if kill -0 \"$efferva_pid\" 2>/dev/null && "
            f"[ \"$efferva_sha\" = {shlex.quote(app_server_launch_sha256)} ]; then "
            "exit 0; fi; "
            f"kill \"$efferva_pid\" 2>/dev/null || true; fi; "
            f"cd {shlex.quote(self._settings.workspace_path)} && "
            f"{shlex.quote(sandbox_binary)} app-server "
            f"{app_server_cli_config}"
            f"--listen {shlex.quote(listen)} "
            f"--ws-auth capability-token "
            f"--ws-token-file {shlex.quote(websocket_token_file)} "
            f"</dev/null >>{shlex.quote(log_file)} 2>&1 & "
            f"echo \"$! {app_server_launch_sha256}\" >{shlex.quote(pid_file)}"
        )
        environment = {
            "CODEX_HOME": codex_home,
            "HOME": self._settings.session_volume_path,
        }
        if effective_api_key:
            environment["OPENAI_API_KEY"] = effective_api_key
        await self._run_command(
            sandbox,
            ("sh", "-lc", command),
            env=environment,
        )

    @staticmethod
    async def _run_command(
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


def _websocket_url(endpoint: str) -> str:
    if endpoint.startswith("https://"):
        return "wss://" + endpoint.removeprefix("https://")
    if endpoint.startswith("http://"):
        return "ws://" + endpoint.removeprefix("http://")
    if endpoint.startswith(("ws://", "wss://")):
        return endpoint
    return f"ws://{endpoint}"
