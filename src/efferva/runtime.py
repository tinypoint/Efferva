from __future__ import annotations

import asyncio
import json
import os
import posixpath
import re
import secrets
import shlex
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from websockets.asyncio.client import ClientConnection, connect

from efferva.config import Settings
from efferva.sandbox import SandboxControlPlane, SandboxEnvironment


class CodexRpcError(RuntimeError):
    def __init__(self, method: str, error: dict[str, Any]) -> None:
        self.method = method
        self.error = error
        super().__init__(f"{method}: {error.get('message', error)}")


ServerRequestHandler = Callable[
    [Mapping[str, Any], str, Mapping[str, Any]],
    Mapping[str, Any] | Awaitable[Mapping[str, Any]],
]


class CodexProxy:
    """A stateless router to the long-lived Codex app-server inside each Sandbox."""

    def __init__(
        self,
        binary: Path,
        settings: Settings,
        sandboxes: SandboxControlPlane,
        *,
        developer_instructions: str | None = None,
        codex_config: Mapping[str, Any] | None = None,
        native_memory_enabled: bool = False,
        server_request_handler: ServerRequestHandler | None = None,
    ) -> None:
        self._binary_bytes = binary.read_bytes()
        self._binary_sha256 = sha256(self._binary_bytes).hexdigest()
        self._settings = settings
        self._sandboxes = sandboxes
        self._developer_instructions = developer_instructions
        self._codex_config = deepcopy(dict(codex_config or {}))
        self._native_memory_enabled = native_memory_enabled
        self._server_request_handler = server_request_handler
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._sessions: dict[UUID, SandboxEnvironment] = {}
        self._connection_targets: dict[str, tuple[str, dict[str, str]]] = {}
        self._next_id = 1
        self._id_lock = asyncio.Lock()

    def set_server_request_handler(
        self,
        handler: ServerRequestHandler | None,
    ) -> None:
        self._server_request_handler = handler

    async def request(
        self,
        session: Mapping[str, Any],
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sandbox = await self._ensure_session(session)
        async with self._connection(sandbox) as websocket:
            return await self._rpc(websocket, method, params or {})

    async def list_threads(self, session: Mapping[str, Any]) -> list[dict[str, Any]]:
        result = await self.request(
            session,
            "thread/list",
            {
                "limit": 100,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "sourceKinds": ["vscode"],
            },
        )
        return list(result.get("data") or [])

    async def delete_thread(
        self,
        session: Mapping[str, Any],
        thread_id: str,
    ) -> None:
        await self.request(
            session,
            "thread/delete",
            {"threadId": thread_id},
        )

    async def list_models(self, session: Mapping[str, Any]) -> list[dict[str, Any]]:
        result = await self.request(
            session,
            "model/list",
            {"limit": 100, "includeHidden": False},
        )
        return list(result.get("data") or [])

    async def list_skills(
        self,
        session: Mapping[str, Any],
        *,
        workspace: str | None = None,
    ) -> list[dict[str, Any]]:
        result = await self.request(
            session,
            "skills/list",
            {
                "cwds": [workspace] if workspace else [],
                "forceReload": False,
            },
        )
        return list(result.get("data") or [])

    async def search_files(
        self,
        session: Mapping[str, Any],
        query: str,
        *,
        workspace: str | None = None,
    ) -> list[dict[str, Any]]:
        workspace = posixpath.normpath(workspace or self._settings.workspace_path)
        if not workspace.startswith("/"):
            raise ValueError("File search workspace must be an absolute Sandbox path")
        result = await self.request(
            session,
            "fuzzyFileSearch",
            {
                "query": query,
                "roots": [workspace],
                "cancellationToken": f"efferva:{session['id']}",
            },
        )
        return list(result.get("files") or [])

    async def set_plan_mode(
        self,
        session: Mapping[str, Any],
        thread_id: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        await self.request(
            session,
            "thread/resume",
            {
                "threadId": thread_id,
                **self._thread_params(
                    model=model,
                    reasoning_effort=reasoning_effort,
                ),
            },
        )
        await self.request(
            session,
            "thread/settings/update",
            {
                "threadId": thread_id,
                "collaborationMode": await self._collaboration_mode(
                    session,
                    "plan",
                    model=model,
                    reasoning_effort=reasoning_effort,
                ),
            },
        )

    async def get_goal(
        self,
        session: Mapping[str, Any],
        thread_id: str,
    ) -> dict[str, Any] | None:
        result = await self.request(
            session,
            "thread/goal/get",
            {"threadId": thread_id},
        )
        goal = result.get("goal")
        return dict(goal) if isinstance(goal, dict) else None

    async def set_goal(
        self,
        session: Mapping[str, Any],
        thread_id: str,
        *,
        objective: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id}
        if objective is not None:
            params["objective"] = objective
        if status is not None:
            params["status"] = status
        result = await self.request(session, "thread/goal/set", params)
        return dict(result["goal"])

    async def clear_goal(
        self,
        session: Mapping[str, Any],
        thread_id: str,
    ) -> bool:
        result = await self.request(
            session,
            "thread/goal/clear",
            {"threadId": thread_id},
        )
        return bool(result.get("cleared"))

    async def start_thread(
        self,
        session: Mapping[str, Any],
        *,
        workspace: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        dynamic_tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        workspace = posixpath.normpath(workspace or self._settings.workspace_path)
        if not workspace.startswith("/"):
            raise ValueError("Thread workspace must be an absolute Sandbox path")
        sandbox = await self._ensure_session(session)
        params = self._thread_params(
            model=model,
            reasoning_effort=reasoning_effort,
            dynamic_tools=dynamic_tools,
        )
        params["cwd"] = workspace
        params["historyMode"] = "paginated"
        async with self._connection(sandbox) as websocket:
            await self._rpc(
                websocket,
                "fs/createDirectory",
                {"path": workspace, "recursive": True},
            )
            result = await self._rpc(websocket, "thread/start", params)
            thread = dict(result["thread"])
        return thread

    async def read_thread(
        self,
        session: Mapping[str, Any],
        thread_id: str,
    ) -> dict[str, Any]:
        result = await self.request(
            session,
            "thread/read",
            {"threadId": thread_id, "includeTurns": False},
        )
        thread = dict(result["thread"])
        turns: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, Any] = {
                "threadId": thread_id,
                "limit": 100,
                "sortDirection": "asc",
                "itemsView": "full",
            }
            if cursor is not None:
                params["cursor"] = cursor
            try:
                page = await self.request(session, "thread/turns/list", params)
            except CodexRpcError as error:
                message = str(error.error.get("message", ""))
                if "is not materialized yet" not in message:
                    raise
                break
            turns.extend(dict(turn) for turn in page.get("data") or [])
            next_cursor = page.get("nextCursor")
            if not isinstance(next_cursor, str) or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        thread["turns"] = turns
        return thread

    async def find_active_turn(
        self,
        session: Mapping[str, Any],
        thread_id: str,
    ) -> str | None:
        thread = await self.read_thread(session, thread_id)
        for turn in reversed(thread.get("turns") or []):
            if turn.get("status") == "inProgress" and turn.get("id"):
                return str(turn["id"])
        return None

    async def interrupt_turn(
        self,
        session: Mapping[str, Any],
        thread_id: str,
        turn_id: str,
    ) -> None:
        await self.request(
            session,
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
        )

    async def steer_turn(
        self,
        session: Mapping[str, Any],
        thread_id: str,
        turn_id: str,
        prompt: str,
    ) -> str:
        result = await self.request(
            session,
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "clientUserMessageId": f"efferva-steer-{uuid4()}",
                "input": await self._input_items(session, prompt),
            },
        )
        return str(result["turnId"])

    async def stream_turn(
        self,
        session: Mapping[str, Any],
        thread_id: str,
        prompt: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        collaboration_mode: str | None = None,
        extra_inputs: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        sandbox = await self._ensure_session(session)
        input_items = await self._input_items(session, prompt, extra_inputs)
        async with self._connection(sandbox) as websocket:
            await self._rpc(
                websocket,
                "thread/resume",
                {
                    "threadId": thread_id,
                    **self._thread_params(
                        model=model,
                        reasoning_effort=reasoning_effort,
                    ),
                },
            )
            async for notification in self._start_turn_on_connection(
                websocket,
                session,
                thread_id,
                input_items,
                model=model,
                reasoning_effort=reasoning_effort,
                collaboration_mode=collaboration_mode,
            ):
                yield notification

    async def stream_new_turn(
        self,
        session: Mapping[str, Any],
        prompt: str,
        *,
        workspace: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        collaboration_mode: str | None = None,
        dynamic_tools: list[dict[str, Any]] | None = None,
        extra_inputs: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        workspace = posixpath.normpath(workspace or self._settings.workspace_path)
        if not workspace.startswith("/"):
            raise ValueError("Thread workspace must be an absolute Sandbox path")
        sandbox = await self._ensure_session(session)
        input_items = await self._input_items(session, prompt, extra_inputs)
        async with self._connection(sandbox) as websocket:
            await self._rpc(
                websocket,
                "fs/createDirectory",
                {"path": workspace, "recursive": True},
            )
            thread_params = self._thread_params(
                model=model,
                reasoning_effort=reasoning_effort,
                dynamic_tools=dynamic_tools,
            )
            thread_params["cwd"] = workspace
            thread_params["historyMode"] = "paginated"
            result = await self._rpc(websocket, "thread/start", thread_params)
            thread = dict(result["thread"])
            thread_id = str(thread["id"])
            yield {
                "method": "efferva/thread-created",
                "params": {"thread": thread},
            }
            async for notification in self._start_turn_on_connection(
                websocket,
                session,
                thread_id,
                input_items,
                model=model,
                reasoning_effort=reasoning_effort,
                collaboration_mode=collaboration_mode,
            ):
                yield notification

    async def _start_turn_on_connection(
        self,
        websocket: ClientConnection,
        session: Mapping[str, Any],
        thread_id: str,
        input_items: list[dict[str, Any]],
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        collaboration_mode: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        request_id = await self._request_id()
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": input_items,
        }
        if model:
            params["model"] = model
        if reasoning_effort:
            params["effort"] = reasoning_effort
        if collaboration_mode:
            params["collaborationMode"] = await self._collaboration_mode(
                session,
                collaboration_mode,
                model=model,
                reasoning_effort=reasoning_effort,
            )
        await websocket.send(
            json.dumps(
                {"method": "turn/start", "id": request_id, "params": params},
                separators=(",", ":"),
            )
        )
        turn_id: str | None = None
        while True:
            message = json.loads(await websocket.recv())
            if message.get("id") == request_id:
                if "error" in message:
                    raise CodexRpcError("turn/start", message["error"])
                turn = message.get("result", {}).get("turn", {})
                turn_id = str(turn.get("id")) if turn.get("id") else None
                yield {
                    "method": "efferva/turn-started",
                    "params": {"threadId": thread_id, "turnId": turn_id},
                }
                continue
            if "method" in message and "id" in message:
                await self._handle_server_request(websocket, message, session=session)
                continue
            if "method" not in message:
                continue
            yield message
            if message["method"] == "turn/completed":
                completed = message.get("params", {}).get("turn", {})
                completed_id = completed.get("id")
                if turn_id is None or completed_id in {None, turn_id}:
                    return

    async def resume_turn(
        self,
        session: Mapping[str, Any],
        thread_id: str,
        turn_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        sandbox = await self._ensure_session(session)
        async with self._connection(sandbox) as websocket:
            request_id = await self._request_id()
            await websocket.send(
                json.dumps(
                    {
                        "method": "thread/resume",
                        "id": request_id,
                        "params": {"threadId": thread_id},
                    },
                    separators=(",", ":"),
                )
            )
            pending_notifications: list[dict[str, Any]] = []
            turn_is_active = False
            while True:
                message = json.loads(await websocket.recv())
                if message.get("id") == request_id:
                    if "error" in message:
                        raise CodexRpcError("thread/resume", message["error"])
                    thread = dict(message.get("result", {}).get("thread") or {})
                    resumed_turn = next(
                        (
                            turn
                            for turn in reversed(thread.get("turns") or [])
                            if str(turn.get("id")) == turn_id
                        ),
                        None,
                    )
                    turn_is_active = bool(
                        resumed_turn
                        and resumed_turn.get("status") == "inProgress"
                    )
                    yield {
                        "method": "efferva/thread-resumed",
                        "params": {
                            "threadId": thread_id,
                            "thread": thread,
                            "turn": resumed_turn,
                            "active": turn_is_active,
                        },
                    }
                    for notification in pending_notifications:
                        yield notification
                    if not turn_is_active:
                        return
                    break
                if "method" in message and "id" in message:
                    await self._handle_server_request(websocket, message, session=session)
                    continue
                if "method" in message:
                    pending_notifications.append(message)

            while True:
                message = json.loads(await websocket.recv())
                if "method" in message and "id" in message:
                    await self._handle_server_request(websocket, message, session=session)
                    continue
                if "method" not in message:
                    continue
                yield message
                if message["method"] == "turn/completed":
                    completed = message.get("params", {}).get("turn", {})
                    if completed.get("id") in {None, turn_id}:
                        return

    async def _input_items(
        self,
        session: Mapping[str, Any],
        prompt: str,
        extra_inputs: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = [
            {"type": "text", "text": prompt, "textElements": []}
        ]
        items.extend(dict(item) for item in (extra_inputs or []))
        mentioned_paths = set(
            re.findall(r"(?<![\w@])@([^\s]+)", prompt)
        )
        for path in mentioned_paths:
            items.append(
                {
                    "type": "mention",
                    "name": posixpath.basename(path.rstrip("/")) or path,
                    "path": path,
                }
            )
        mentioned_skill_names = set(
            re.findall(r"(?<![\w$])\$([A-Za-z0-9:_-]+)", prompt)
        )
        if not mentioned_skill_names:
            return items
        skill_entries = await self.list_skills(session)
        skills_by_name = {
            str(skill.get("name")): skill
            for entry in skill_entries
            for skill in entry.get("skills", [])
            if skill.get("enabled") and skill.get("name") and skill.get("path")
        }
        for name in mentioned_skill_names:
            skill = skills_by_name.get(name)
            if skill is not None:
                items.append(
                    {
                        "type": "skill",
                        "name": name,
                        "path": skill["path"],
                    }
                )
        return items

    async def _collaboration_mode(
        self,
        session: Mapping[str, Any],
        name: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        result = await self.request(session, "collaborationMode/list")
        modes = list(result.get("data") or [])
        selected = next(
            (
                item
                for item in modes
                if str(item.get("name", "")).casefold() == name.casefold()
                or str(item.get("mode", "")).casefold() == name.casefold()
            ),
            None,
        )
        if selected is None:
            raise ValueError(f"Codex collaboration mode is unavailable: {name}")
        selected_model = selected.get("model") or model or self._settings.codex_model
        if not selected_model:
            raise ValueError(f"Codex collaboration mode {name} has no model")
        selected_effort = selected.get("reasoning_effort")
        if selected_effort is None:
            selected_effort = reasoning_effort
        return {
            "mode": selected.get("mode") or name.casefold(),
            "settings": {
                "model": selected_model,
                "reasoning_effort": selected_effort,
                "developer_instructions": None,
            },
        }

    async def _ensure_session(
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
            sandbox = await self._sandboxes.ensure(
                session_id,
            )
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
        app_server_launch_sha256 = sha256(
            json.dumps(
                {
                    "binary": self._binary_sha256,
                    "config": app_server_config_args,
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
        api_key = os.environ.get("OPENAI_API_KEY")
        if api_key:
            environment["OPENAI_API_KEY"] = (
                "efferva-credential-proxy"
                if sandbox.sandbox.state.get("credentialProxy")
                else api_key
            )
        await self._run_command(
            sandbox,
            ("sh", "-lc", command),
            env=environment,
        )

    async def _run_command(
        self,
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

    @asynccontextmanager
    async def _connection(
        self,
        sandbox: SandboxEnvironment,
    ) -> AsyncIterator[ClientConnection]:
        target = self._connection_targets.get(sandbox.environment_id)
        if target is None:
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
        url, connection_headers = target
        last_error: Exception | None = None
        for attempt in range(8):
            try:
                websocket = await connect(
                    url,
                    additional_headers=connection_headers,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=16 * 1024 * 1024,
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
            await self._initialize(websocket)
            yield websocket
        finally:
            await websocket.close()

    async def _initialize(self, websocket: ClientConnection) -> None:
        await self._rpc(
            websocket,
            "initialize",
            {
                "clientInfo": {
                    "name": "efferva",
                    "title": "Efferva",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "requestAttestation": False,
                },
            },
        )
        await websocket.send(
            json.dumps(
                {"method": "initialized", "params": {}},
                separators=(",", ":"),
            )
        )

    async def _rpc(
        self,
        websocket: ClientConnection,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = await self._request_id()
        await websocket.send(
            json.dumps(
                {"method": method, "id": request_id, "params": params},
                separators=(",", ":"),
            )
        )
        while True:
            message = json.loads(await websocket.recv())
            if message.get("id") == request_id:
                if "error" in message:
                    raise CodexRpcError(method, message["error"])
                return dict(message.get("result") or {})
            if "method" in message and "id" in message:
                await self._handle_server_request(websocket, message)

    async def _handle_server_request(
        self,
        websocket: ClientConnection,
        message: Mapping[str, Any],
        *,
        session: Mapping[str, Any] | None = None,
    ) -> None:
        method = str(message["method"])
        raw_params = message.get("params")
        params = raw_params if isinstance(raw_params, Mapping) else {}
        try:
            if self._server_request_handler is not None:
                if session is None:
                    raise RuntimeError(
                        f"server request {method} has no Efferva Session context"
                    )
                response = self._server_request_handler(session, method, params)
                if isinstance(response, Awaitable):
                    response = await response
                payload: dict[str, Any] = {
                    "id": message["id"],
                    "result": dict(response),
                }
            else:
                default = _default_server_response(method, params)
                if default is None:
                    raise NotImplementedError(f"unsupported server request: {method}")
                payload = {"id": message["id"], "result": default}
        except Exception as error:
            payload = {
                "id": message["id"],
                "error": {"code": -32000, "message": str(error)},
            }
        await websocket.send(
            json.dumps(
                payload,
                separators=(",", ":"),
            )
        )

    async def _request_id(self) -> int:
        async with self._id_lock:
            value = self._next_id
            self._next_id += 1
            return value

    def _thread_params(
        self,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        dynamic_tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        config = deepcopy(self._codex_config)
        if self._settings.codex_openai_base_url:
            providers = config.setdefault("model_providers", {})
            providers["efferva_proxy"] = {
                "name": "Efferva LLM proxy",
                "base_url": self._settings.codex_openai_base_url,
                "env_key": "OPENAI_API_KEY",
                "wire_api": "responses",
            }
            config["model_provider"] = "efferva_proxy"
        if not self._native_memory_enabled:
            config.setdefault("features", {})["memories"] = False
        params: dict[str, Any] = {
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        selected_model = model or self._settings.codex_model
        if selected_model:
            params["model"] = selected_model
        if reasoning_effort:
            params["effort"] = reasoning_effort
        if self._settings.codex_openai_base_url:
            params["modelProvider"] = "efferva_proxy"
        if self._developer_instructions:
            params["developerInstructions"] = self._developer_instructions
        if config:
            params["config"] = config
        if dynamic_tools:
            params["dynamicTools"] = _normalize_dynamic_tools(dynamic_tools)
        return params


def _normalize_dynamic_tools(
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw_tool in tools:
        source = raw_tool.get("function")
        tool = source if isinstance(source, Mapping) else raw_tool
        name = str(tool.get("name") or "").strip()
        if not name:
            raise ValueError("dynamic tool name is required")
        input_schema = (
            tool.get("inputSchema")
            or tool.get("input_schema")
            or tool.get("parameters")
            or {"type": "object", "properties": {}}
        )
        normalized.append(
            {
                "type": "function",
                "name": name,
                "description": str(tool.get("description") or name),
                "inputSchema": input_schema,
                "deferLoading": bool(
                    tool.get("deferLoading") or tool.get("defer_loading")
                ),
            }
        )
    return normalized


def _default_server_response(
    method: str,
    params: Mapping[str, Any],
) -> dict[str, Any] | None:
    if method == "item/tool/call":
        tool = str(params.get("tool") or "dynamic tool")
        return {
            "contentItems": [
                {
                    "type": "inputText",
                    "text": f"No Efferva handler is registered for {tool}.",
                }
            ],
            "success": False,
        }
    if method == "item/tool/requestUserInput":
        questions = params.get("questions") or []
        return {
            "answers": {
                str(question["id"]): {"answers": []}
                for question in questions
                if isinstance(question, Mapping) and question.get("id")
            }
        }
    if method in {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }:
        return {"decision": "decline"}
    if method == "mcpServer/elicitation/request":
        return {"action": "decline", "content": None, "_meta": None}
    if method == "item/permissions/requestApproval":
        return {"permissions": {}, "scope": "turn"}
    return None


def _websocket_url(endpoint: str) -> str:
    if endpoint.startswith("https://"):
        return "wss://" + endpoint.removeprefix("https://")
    if endpoint.startswith("http://"):
        return "ws://" + endpoint.removeprefix("http://")
    if endpoint.startswith(("ws://", "wss://")):
        return endpoint
    return f"ws://{endpoint}"
