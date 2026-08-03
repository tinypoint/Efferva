from __future__ import annotations

import asyncio
import json
import posixpath
import secrets
import shlex
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from hashlib import sha256
from typing import Any
from uuid import UUID

from websockets.asyncio.client import ClientConnection, connect as connect_websocket

from efferva.config import CodexConfig, SandboxLayout
from efferva.db import Database
from efferva.sandbox.protocol import SandboxEnvironment
from efferva.sandbox.service import SessionSandboxService

_CODEX_VERSION = "0.146.0"
_CODEX_RELEASES = {
    "aarch64": (
        "aarch64-unknown-linux-musl",
        "975bac91562abeedeb8f79636d51a86649b31f34a9de6a3bcb059565b6cf1f87",
    ),
    "x86_64": (
        "x86_64-unknown-linux-musl",
        "5ba3b9405543953081f661d0854d266f76e2abbe51d41349355a36de7673776a",
    ),
}


class CodexAppServerManager:
    """Installs and starts one Codex app-server per Efferva Session."""

    def __init__(
        self,
        config: CodexConfig,
        layout: SandboxLayout,
        sandboxes: SessionSandboxService,
        database: Database,
    ) -> None:
        self._config = config
        self._layout = layout
        self._sandboxes = sandboxes
        self._database = database

    @asynccontextmanager
    async def connect(
        self,
        session: Mapping[str, Any],
    ) -> AsyncIterator[ClientConnection]:
        url, headers = await self._connection_target(session)
        last_error: Exception | None = None
        for attempt in range(8):
            try:
                websocket = await connect_websocket(
                    url,
                    additional_headers=headers,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=128 * 1024 * 1024,
                )
                break
            except Exception as error:
                last_error = error
                if attempt == 7:
                    raise RuntimeError(
                        f"Codex app-server is not reachable at {url}: {error}"
                    ) from error
                await asyncio.sleep(0.1 * (2**attempt))
        else:
            raise RuntimeError(str(last_error))
        try:
            yield websocket
        finally:
            await websocket.close()

    async def _connection_target(
        self,
        session: Mapping[str, Any],
    ) -> tuple[str, dict[str, str]]:
        sandbox = await self._ensure(session)
        return await self._sandbox_connection_target(sandbox)

    async def _sandbox_connection_target(
        self,
        sandbox: SandboxEnvironment,
    ) -> tuple[str, dict[str, str]]:
        endpoint, headers = await sandbox.runtime.get_endpoint(
            self._config.appserver_port
        )
        websocket_token = (
            (
                await sandbox.runtime.read_file(
                    posixpath.join(
                        self._layout.codex_home_path,
                        "app-server.token",
                    )
                )
            )
            .decode("utf-8")
            .strip()
        )
        return (
            _websocket_url(endpoint),
            {
                **headers,
                "Authorization": f"Bearer {websocket_token}",
            },
        )

    async def _ensure(
        self,
        session: Mapping[str, Any],
    ) -> SandboxEnvironment:
        session_id = UUID(str(session["id"]))
        sandbox = await self._sandboxes.ensure(session)
        config_args, effective_api_key, launch_sha256 = (
            self._launch_configuration(sandbox)
        )
        advisory_lock_key = f"efferva:codex-appserver-session:{session_id}"
        async with self._database.advisory_lock(advisory_lock_key):
            if not await self._is_running(sandbox, launch_sha256):
                await self._install_and_start(
                    sandbox,
                    config_args=config_args,
                    effective_api_key=effective_api_key,
                    launch_sha256=launch_sha256,
                )
        return sandbox

    async def _is_running(
        self,
        sandbox: SandboxEnvironment,
        launch_sha256: str,
    ) -> bool:
        try:
            pid_file = "/tmp/efferva-app-server.pid"
            await sandbox.runtime.stat(pid_file)
            pid_record = (await sandbox.runtime.read_file(pid_file)).decode().split()
            if len(pid_record) != 2 or pid_record[1] != launch_sha256:
                return False
            url, headers = await self._sandbox_connection_target(sandbox)
            websocket = await connect_websocket(
                url,
                additional_headers=headers,
                open_timeout=1,
                ping_interval=None,
                max_size=128 * 1024 * 1024,
            )
            await websocket.close()
            return True
        except Exception:
            return False

    def _launch_configuration(
        self,
        sandbox: SandboxEnvironment,
    ) -> tuple[tuple[str, ...], str | None, str]:
        app_server_overrides: dict[str, str] = {}
        if self._config.openai_base_url:
            app_server_overrides = {
                "model_providers.efferva_proxy.name": "Efferva LLM proxy",
                "model_providers.efferva_proxy.base_url": (
                    self._config.openai_base_url
                ),
                "model_providers.efferva_proxy.env_key": "OPENAI_API_KEY",
                "model_providers.efferva_proxy.wire_api": "responses",
                "model_provider": "efferva_proxy",
            }
        config_args = tuple(
            f"{key}={json.dumps(value)}" for key, value in app_server_overrides.items()
        )
        api_key = self._config.api_key
        effective_api_key = None
        if api_key:
            effective_api_key = (
                "efferva-credential-proxy"
                if sandbox.sandbox.state.get("credentialProxy")
                else api_key
            )
        launch_sha256 = sha256(
            json.dumps(
                {
                    "codexVersion": _CODEX_VERSION,
                    "config": config_args,
                    "uid": self._layout.uid,
                    "gid": self._layout.gid,
                    "openaiApiKeySha256": (
                        sha256(effective_api_key.encode()).hexdigest()
                        if effective_api_key
                        else None
                    ),
                },
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return config_args, effective_api_key, launch_sha256

    async def _install_and_start(
        self,
        sandbox: SandboxEnvironment,
        *,
        config_args: tuple[str, ...],
        effective_api_key: str | None,
        launch_sha256: str,
    ) -> None:
        target, archive_sha256 = await self._codex_release_for_sandbox(sandbox)
        runtime_root = posixpath.join(
            self._layout.codex_runtime_dir,
            _CODEX_VERSION,
            target,
        )
        sandbox_binary = posixpath.join(runtime_root, "codex")
        try:
            await sandbox.runtime.stat(sandbox_binary)
        except FileNotFoundError:
            await self._download_codex(
                sandbox,
                runtime_root=runtime_root,
                sandbox_binary=sandbox_binary,
                target=target,
                archive_sha256=archive_sha256,
            )

        codex_home = self._layout.codex_home_path
        websocket_token_file = posixpath.join(codex_home, "app-server.token")
        websocket_token = secrets.token_urlsafe(32)
        pid_file = "/tmp/efferva-app-server.pid"
        log_file = posixpath.join(codex_home, "app-server.log")
        listen = f"ws://0.0.0.0:{self._config.appserver_port}"
        privileged_bootstrap = (
            "if id sandbox >/dev/null 2>&1; then "
            f'[ "$(id -u sandbox)" = {self._layout.uid} ] || '
            "{ echo 'sandbox user has an unexpected UID' >&2; exit 1; }; "
            "elif command -v useradd >/dev/null 2>&1; then "
            f"useradd -u {self._layout.uid} "
            f"-d {shlex.quote(self._layout.session_volume_path)} "
            "-M -s /bin/sh sandbox; "
            "elif command -v adduser >/dev/null 2>&1; then "
            f"adduser -D -u {self._layout.uid} "
            f"-h {shlex.quote(self._layout.session_volume_path)} "
            "sandbox; "
            "else echo 'sandbox image cannot create users' >&2; exit 1; fi; "
            f"chmod 755 {shlex.quote(sandbox_binary)} && "
            f"chown {self._layout.uid}:{self._layout.gid} "
            f"{shlex.quote(self._layout.session_volume_path)}"
        )
        await self._run_command(sandbox, ("sh", "-lc", privileged_bootstrap))
        sandbox_bootstrap = (
            f"mkdir -p {shlex.quote(codex_home)} "
            f"{shlex.quote(self._layout.workspace_path)} && "
            f"if [ ! -s {shlex.quote(websocket_token_file)} ]; then "
            "umask 077; "
            f"printf '%s\\n' {shlex.quote(websocket_token)} "
            f">{shlex.quote(websocket_token_file)}; fi && "
            f"touch {shlex.quote(log_file)} && "
            f"chmod 600 {shlex.quote(websocket_token_file)} "
            f"{shlex.quote(log_file)}"
        )
        await self._run_command(
            sandbox,
            ("sh", "-lc", sandbox_bootstrap),
            uid=self._layout.uid,
            gid=self._layout.gid,
        )
        app_server_cli_config = "".join(
            f"-c {shlex.quote(argument)} " for argument in config_args
        )
        reconcile_command = (
            f"if [ -s {shlex.quote(pid_file)} ]; then "
            f"read -r efferva_pid efferva_sha <{shlex.quote(pid_file)}; "
            f'if kill -0 "$efferva_pid" 2>/dev/null && '
            f'[ "$efferva_sha" = {shlex.quote(launch_sha256)} ]; then '
            "exit 0; fi; "
            f'kill "$efferva_pid" 2>/dev/null || true; '
            f"rm -f {shlex.quote(pid_file)}; fi"
        )
        await self._run_command(sandbox, ("sh", "-lc", reconcile_command))
        command = (
            f"if [ -s {shlex.quote(pid_file)} ]; then exit 0; fi; "
            f"cd {shlex.quote(self._layout.workspace_path)} && "
            f"{shlex.quote(sandbox_binary)} app-server "
            f"{app_server_cli_config}"
            f"--listen {shlex.quote(listen)} "
            f"--ws-auth capability-token "
            f"--ws-token-file {shlex.quote(websocket_token_file)} "
            f"</dev/null >>{shlex.quote(log_file)} 2>&1 & "
            f'echo "$! {launch_sha256}" >{shlex.quote(pid_file)}'
        )
        environment = {
            "CODEX_HOME": codex_home,
            "HOME": self._layout.session_volume_path,
        }
        if effective_api_key:
            environment["OPENAI_API_KEY"] = effective_api_key
        await self._run_command(
            sandbox,
            ("sh", "-lc", command),
            env=environment,
            uid=self._layout.uid,
            gid=self._layout.gid,
        )

    async def _codex_release_for_sandbox(
        self,
        sandbox: SandboxEnvironment,
    ) -> tuple[str, str]:
        result = await sandbox.runtime.run_command(("uname", "-m"), cwd="/")
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout).decode(errors="replace")
            raise RuntimeError(detail or "could not determine sandbox architecture")
        architecture = result.stdout.decode().strip().lower()
        architecture = {
            "amd64": "x86_64",
            "arm64": "aarch64",
        }.get(architecture, architecture)
        release = _CODEX_RELEASES.get(architecture)
        if release is None:
            raise RuntimeError(f"unsupported sandbox architecture: {architecture}")
        return release

    async def _download_codex(
        self,
        sandbox: SandboxEnvironment,
        *,
        runtime_root: str,
        sandbox_binary: str,
        target: str,
        archive_sha256: str,
    ) -> None:
        asset_name = f"codex-{target}.tar.gz"
        url = (
            "https://github.com/openai/codex/releases/download/"
            f"rust-v{_CODEX_VERSION}/{asset_name}"
        )
        archive = posixpath.join(runtime_root, f".{asset_name}.tmp")
        temporary_binary = posixpath.join(runtime_root, ".codex.tmp")
        python_download = (
            "import sys,urllib.request;"
            "urllib.request.urlretrieve(sys.argv[1],sys.argv[2])"
        )
        python_sha256 = (
            "import hashlib,sys;"
            "print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())"
        )
        expected_member = f"codex-{target}"
        command = (
            f"mkdir -p {shlex.quote(runtime_root)} && "
            f"rm -f {shlex.quote(archive)} {shlex.quote(temporary_binary)} && "
            "if command -v curl >/dev/null 2>&1; then "
            f"curl -fsSL {shlex.quote(url)} -o {shlex.quote(archive)}; "
            "elif command -v wget >/dev/null 2>&1; then "
            f"wget -qO {shlex.quote(archive)} {shlex.quote(url)}; "
            "elif command -v python3 >/dev/null 2>&1; then "
            f"python3 -c {shlex.quote(python_download)} "
            f"{shlex.quote(url)} {shlex.quote(archive)}; "
            "elif command -v python >/dev/null 2>&1; then "
            f"python -c {shlex.quote(python_download)} "
            f"{shlex.quote(url)} {shlex.quote(archive)}; "
            "else echo 'sandbox needs curl, wget, or Python to download Codex' >&2; "
            "exit 1; fi && "
            "if command -v sha256sum >/dev/null 2>&1; then "
            f"actual_sha256=$(sha256sum {shlex.quote(archive)} | awk '{{print $1}}'); "
            "elif command -v shasum >/dev/null 2>&1; then "
            f"actual_sha256=$(shasum -a 256 {shlex.quote(archive)} | awk '{{print $1}}'); "
            "elif command -v python3 >/dev/null 2>&1; then "
            f"actual_sha256=$(python3 -c {shlex.quote(python_sha256)} "
            f"{shlex.quote(archive)}); "
            "elif command -v python >/dev/null 2>&1; then "
            f"actual_sha256=$(python -c {shlex.quote(python_sha256)} "
            f"{shlex.quote(archive)}); "
            "else echo 'sandbox cannot calculate SHA-256' >&2; exit 1; fi; "
            f'if [ "$actual_sha256" != {shlex.quote(archive_sha256)} ]; then '
            f"echo 'checksum mismatch for {asset_name}' >&2; exit 1; fi; "
            "command -v tar >/dev/null 2>&1 || "
            "{ echo 'sandbox needs tar to extract Codex' >&2; exit 1; }; "
            f"member=$(tar -tzf {shlex.quote(archive)} | "
            f"awk -F/ -v expected={shlex.quote(expected_member)} "
            '\'$NF == "codex" || $NF == expected '
            "{ count++; found=$0 } END { if (count == 1) print found; else exit 1 }') "
            f"|| {{ echo 'invalid Codex archive {asset_name}' >&2; exit 1; }}; "
            f'tar -xOzf {shlex.quote(archive)} "$member" '
            f">{shlex.quote(temporary_binary)} && "
            f"chmod 755 {shlex.quote(temporary_binary)} && "
            f"mv {shlex.quote(temporary_binary)} {shlex.quote(sandbox_binary)} && "
            f"rm -f {shlex.quote(archive)}"
        )
        await self._run_command(sandbox, ("sh", "-lc", command))

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
