from __future__ import annotations

import asyncio
import json
import logging
import os
import posixpath
import re
import time
from collections.abc import AsyncIterator, Mapping
from copy import deepcopy
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest
from urllib.request import urlopen
from uuid import uuid4

from efferva.codex_rpc import (
    CodexConnection,
    CodexRpcClient,
    CodexRpcError,
    ServerRequestHandler,
)
from efferva.config import Settings


_LOGGER = logging.getLogger(__name__)

_THREAD_NAME_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 36,
        }
    },
    "required": ["title"],
    "additionalProperties": False,
}

_THREAD_NAME_INSTRUCTIONS = """\
You generate a user-facing title for a coding-agent thread.
Return only the structured output requested by the response schema.
Do not answer the user's request, inspect files, call tools, or perform any work.
The title must describe the user's task, use the user's language, contain at most
36 characters, and have no quotes, Markdown, or trailing punctuation. Reuse the
prompt when it is already a concise title; otherwise prefer a precise action or
question title.
"""


class CodexGateway:
    """Product operations backed by Codex app-server RPCs."""

    def __init__(
        self,
        settings: Settings,
        rpc: CodexRpcClient,
        *,
        developer_instructions: str | None = None,
        codex_config: Mapping[str, Any] | None = None,
        native_memory_enabled: bool = False,
    ) -> None:
        self._settings = settings
        self._rpc = rpc
        self._developer_instructions = developer_instructions
        self._codex_config = deepcopy(dict(codex_config or {}))
        self._native_memory_enabled = native_memory_enabled

    def set_server_request_handler(
        self,
        handler: ServerRequestHandler | None,
    ) -> None:
        self._rpc.set_server_request_handler(handler)

    async def _request(
        self,
        session: Mapping[str, Any],
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._rpc.request(session, method, params)

    async def list_threads(self, session: Mapping[str, Any]) -> list[dict[str, Any]]:
        result = await self._request(
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
        await self._request(
            session,
            "thread/delete",
            {"threadId": thread_id},
        )

    async def _set_thread_name(
        self,
        session: Mapping[str, Any],
        thread_id: str,
        name: str,
    ) -> None:
        await self._request(
            session,
            "thread/name/set",
            {"threadId": thread_id, "name": name},
        )

    async def list_models(self, session: Mapping[str, Any]) -> list[dict[str, Any]]:
        result = await self._request(
            session,
            "model/list",
            {"limit": 100, "includeHidden": False},
        )
        models = list(result.get("data") or [])
        if not self._settings.codex_openai_base_url:
            return models

        provider_models = await asyncio.to_thread(
            self._fetch_provider_model_ids,
        )
        return [
            model
            for model in models
            if str(model.get("model") or model.get("id") or "")
            in provider_models
        ]

    async def get_thread_settings(
        self,
        session: Mapping[str, Any],
        thread_id: str,
    ) -> dict[str, str | None]:
        result = await self._request(
            session,
            "thread/resume",
            {"threadId": thread_id, "excludeTurns": True},
        )
        model = str(result.get("model") or "").strip()
        if not model:
            raise CodexRpcError(
                "thread/resume",
                {"message": "response did not include the effective model"},
            )
        effort = str(result.get("reasoningEffort") or "").strip() or None
        return {"model": model, "reasoning_effort": effort}

    def _fetch_provider_model_ids(self) -> set[str]:
        base_url = str(self._settings.codex_openai_base_url).rstrip("/")
        headers = {"Accept": "application/json"}
        api_key = os.environ.get("OPENAI_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = UrlRequest(f"{base_url}/models", headers=headers)
        try:
            with urlopen(request, timeout=10) as response:
                payload = json.load(response)
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as error:
            raise CodexRpcError(
                "model/list",
                {"message": f"model provider discovery failed: {error}"},
            ) from error
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise CodexRpcError(
                "model/list",
                {"message": "model provider returned an invalid /models response"},
            )
        return {
            str(item["id"])
            for item in data
            if isinstance(item, dict) and item.get("id")
        }

    async def list_skills(
        self,
        session: Mapping[str, Any],
        *,
        workspace: str | None = None,
    ) -> list[dict[str, Any]]:
        result = await self._request(
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
        result = await self._request(
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
        await self._request(
            session,
            "thread/resume",
            {"threadId": thread_id, "excludeTurns": True},
        )
        params: dict[str, Any] = {
            "threadId": thread_id,
            "collaborationMode": await self._collaboration_mode(
                session,
                "plan",
                model=model,
                reasoning_effort=reasoning_effort,
            ),
        }
        if model:
            params["model"] = model
        if reasoning_effort:
            params["effort"] = reasoning_effort
        await self._request(
            session,
            "thread/settings/update",
            params,
        )

    async def get_goal(
        self,
        session: Mapping[str, Any],
        thread_id: str,
    ) -> dict[str, Any] | None:
        result = await self._request(
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
        result = await self._request(session, "thread/goal/set", params)
        return dict(result["goal"])

    async def clear_goal(
        self,
        session: Mapping[str, Any],
        thread_id: str,
    ) -> bool:
        result = await self._request(
            session,
            "thread/goal/clear",
            {"threadId": thread_id},
        )
        return bool(result.get("cleared"))

    async def read_thread(
        self,
        session: Mapping[str, Any],
        thread_id: str,
    ) -> dict[str, Any]:
        result = await self._request(
            session,
            "thread/read",
            {"threadId": thread_id, "includeTurns": False},
        )
        return dict(result["thread"])

    async def list_thread_turns(
        self,
        session: Mapping[str, Any],
        thread_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        sort_direction: str | None = None,
        items_view: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id}
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        if sort_direction is not None:
            params["sortDirection"] = sort_direction
        if items_view is not None:
            params["itemsView"] = items_view
        return await self._request(session, "thread/turns/list", params)

    async def find_active_turn(
        self,
        session: Mapping[str, Any],
        thread_id: str,
    ) -> str | None:
        try:
            page = await self.list_thread_turns(
                session,
                thread_id,
                limit=1,
                sort_direction="desc",
                items_view="notLoaded",
            )
        except CodexRpcError as error:
            message = str(error.error.get("message") or "")
            if "is not materialized yet" in message:
                return None
            raise
        for turn in page.get("data") or []:
            if turn.get("status") == "inProgress" and turn.get("id"):
                return str(turn["id"])
        return None

    async def interrupt_turn(
        self,
        session: Mapping[str, Any],
        thread_id: str,
        turn_id: str,
    ) -> None:
        await self._request(
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
        result = await self._request(
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
        input_items = await self._input_items(session, prompt, extra_inputs)
        async with self._rpc.connection(session) as connection:
            await connection.request(
                "thread/resume",
                {"threadId": thread_id, "excludeTurns": True},
            )
            async for notification in self._start_turn_on_connection(
                connection,
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
        input_items = await self._input_items(session, prompt, extra_inputs)
        async with self._rpc.connection(session) as connection:
            await connection.request(
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
            result = await connection.request("thread/start", thread_params)
            thread = dict(result["thread"])
            thread_id = str(thread["id"])
            thread_name_task = asyncio.create_task(
                self._generate_and_set_thread_name(
                    session,
                    thread_id,
                    prompt,
                    workspace=workspace,
                    model=model,
                )
            )
            name_notification_seen = False
            try:
                yield {
                    "method": "efferva/thread-created",
                    "params": {"thread": thread},
                }
                async for notification in self._start_turn_on_connection(
                    connection,
                    session,
                    thread_id,
                    input_items,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    collaboration_mode=collaboration_mode,
                ):
                    if (
                        notification.get("method") == "thread/name/updated"
                        and str(
                            (notification.get("params") or {}).get("threadId")
                            or ""
                        )
                        == thread_id
                    ):
                        name_notification_seen = True
                    if notification.get("method") == "turn/completed":
                        thread_name = await thread_name_task
                        if thread_name and not name_notification_seen:
                            yield {
                                "method": "thread/name/updated",
                                "params": {
                                    "threadId": thread_id,
                                    "threadName": thread_name,
                                },
                            }
                    yield notification
            finally:
                if not thread_name_task.done():
                    thread_name_task.cancel()
                    await asyncio.gather(
                        thread_name_task,
                        return_exceptions=True,
                    )

    async def _generate_and_set_thread_name(
        self,
        session: Mapping[str, Any],
        thread_id: str,
        prompt: str,
        *,
        workspace: str,
        model: str | None,
    ) -> str | None:
        fallback = _normalize_thread_name(prompt)
        generated: str | None = None
        try:
            async with asyncio.timeout(30):
                async with self._rpc.connection(session) as connection:
                    thread_params = self._thread_params(
                        model=model,
                        reasoning_effort="low",
                    )
                    thread_params.pop("developerInstructions", None)
                    thread_params.pop("dynamicTools", None)
                    thread_params.update(
                        {
                            "cwd": workspace,
                            "sandbox": "read-only",
                            "baseInstructions": _THREAD_NAME_INSTRUCTIONS,
                            "ephemeral": True,
                            "threadSource": "system",
                        }
                    )
                    config = deepcopy(thread_params.get("config") or {})
                    features = config.setdefault("features", {})
                    features.update(
                        {
                            "hooks": False,
                            "multi_agent": False,
                            "multi_agent_v2": False,
                            "plugins": False,
                            "tool_suggest": False,
                        }
                    )
                    config["web_search"] = "disabled"
                    thread_params["config"] = config
                    result = await connection.request(
                        "thread/start",
                        thread_params,
                    )
                    metadata_thread_id = str(result["thread"]["id"])
                    async for notification in self._start_turn_on_connection(
                        connection,
                        session,
                        metadata_thread_id,
                        [
                            {
                                "type": "text",
                                "text": (
                                    "Generate a title for this user prompt:\n\n"
                                    f"{prompt[:2000]}"
                                ),
                            }
                        ],
                        model=model,
                        reasoning_effort="low",
                        output_schema=_THREAD_NAME_OUTPUT_SCHEMA,
                    ):
                        if notification.get("method") != "item/completed":
                            continue
                        item = (notification.get("params") or {}).get("item") or {}
                        if item.get("type") == "agentMessage":
                            generated = _thread_name_from_output(
                                str(item.get("text") or "")
                            )
        except Exception:
            _LOGGER.warning(
                "thread title generation failed for %s",
                thread_id,
                exc_info=True,
            )

        name = generated or fallback
        if not name:
            return None
        try:
            async with asyncio.timeout(5):
                await self._set_thread_name(session, thread_id, name)
        except Exception:
            _LOGGER.warning(
                "thread/name/set failed for %s",
                thread_id,
                exc_info=True,
            )
            return None
        return name

    async def _start_turn_on_connection(
        self,
        connection: CodexConnection,
        session: Mapping[str, Any],
        thread_id: str,
        input_items: list[dict[str, Any]],
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        collaboration_mode: str | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": input_items,
        }
        if model:
            params["model"] = model
        if reasoning_effort:
            params["effort"] = reasoning_effort
        if output_schema is not None:
            params["outputSchema"] = output_schema
        if collaboration_mode:
            params["collaborationMode"] = await self._collaboration_mode(
                session,
                collaboration_mode,
                model=model,
                reasoning_effort=reasoning_effort,
            )
        request_id = await connection.start_request("turn/start", params)
        turn_id: str | None = None
        while True:
            message = await connection.receive()
            if message.get("id") == request_id:
                if "error" in message:
                    raise CodexRpcError("turn/start", message["error"])
                turn = message.get("result", {}).get("turn", {})
                turn_id = str(turn.get("id")) if turn.get("id") else None
                yield {
                    "method": "efferva/turn-started",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "startedAt": turn.get("startedAt") or time.time(),
                    },
                }
                continue
            if "method" in message and "id" in message:
                await connection.handle_server_request(message)
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
        async with self._rpc.connection(session) as connection:
            request_id = await connection.start_request(
                "thread/resume",
                {"threadId": thread_id},
            )
            pending_notifications: list[dict[str, Any]] = []
            turn_is_active = False
            while True:
                message = await connection.receive()
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
                    await connection.handle_server_request(message)
                    continue
                if "method" in message:
                    pending_notifications.append(message)

            while True:
                message = await connection.receive()
                if "method" in message and "id" in message:
                    await connection.handle_server_request(message)
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
        result = await self._request(session, "collaborationMode/list")
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


def _thread_name_from_output(output: str) -> str | None:
    candidate = output
    try:
        parsed = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        pass
    else:
        if isinstance(parsed, Mapping):
            candidate = str(parsed.get("title") or "")
    return _normalize_thread_name(candidate)


def _normalize_thread_name(value: str) -> str | None:
    lines = [line.strip() for line in str(value).splitlines() if line.strip()]
    if not lines:
        return None
    name = re.sub(r"^(?:title|标题)\s*[:：]\s*", "", lines[0], flags=re.I)
    name = name.strip(" \t\r\n`'\"")
    name = re.sub(r"\s+", " ", name).rstrip(".?!。！？")
    if not name:
        return None
    if len(name) > 36:
        name = f"{name[:35].rstrip()}…"
    return name
