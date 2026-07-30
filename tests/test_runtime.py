from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from efferva.capabilities import SkillRoot
from efferva.runtime import CodexRpcError, CodexRuntime
from efferva.sandbox import SandboxEnvironment, SandboxHandle
from efferva.tools import Tool, ToolContext


def sandbox_environment() -> SandboxEnvironment:
    return SandboxEnvironment(
        environment_id="session",
        endpoint="ws://gateway/v1/environments/token",
        workspace_path="/workspace",
        sandbox=SandboxHandle(
            provider="test",
            external_ref="sandbox",
            workspace_id=uuid4(),
        ),
    )


def test_runtime_command_does_not_require_a_base_config() -> None:
    runtime = CodexRuntime(
        Path("/runtime"),
        "postgresql://unused",
        openai_base_url="http://proxy:8317/v1",
        model="gpt-5.4",
    )

    assert runtime._runtime_command() == ["/runtime"]


@pytest.mark.asyncio
async def test_runtime_injects_codex_config_when_starting_and_resuming_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = CodexRuntime(
        Path("/not-started"),
        "postgresql://unused",
        openai_base_url="http://proxy:8317/v1",
        model="gpt-5.4",
        codex_config={
            "model_reasoning_effort": "high",
            "features": {"unified_exec": True},
            "mcp_servers": {
                "vibe_trading": {
                    "command": "vibe-trading-mcp",
                    "args": [],
                    "cwd": "$EFFERVA_SANDBOX_WORKSPACE_PATH",
                    "environment_id": "$EFFERVA_SANDBOX_ENVIRONMENT_ID",
                }
            },
        },
    )
    sandbox = sandbox_environment()
    requests: list[tuple[str, dict[str, Any] | None]] = []

    async def request(
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_timeout: float = 120,
    ) -> dict[str, Any]:
        requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "codex-thread"}}
        return {}

    monkeypatch.setattr(runtime, "request", request)

    assert await runtime.start_thread(sandbox) == "codex-thread"
    await runtime.resume_thread("codex-thread", sandbox)

    expected_config = {
        "model_reasoning_effort": "high",
        "features": {"unified_exec": True, "memories": False},
        "mcp_servers": {
            "vibe_trading": {
                "command": "vibe-trading-mcp",
                "args": [],
                "cwd": "/workspace",
                "environment_id": "session",
            }
        },
        "model_provider": "efferva_proxy",
        "model_providers": {
            "efferva_proxy": {
                "name": "Efferva LLM proxy",
                "base_url": "http://proxy:8317/v1",
                "env_key": "OPENAI_API_KEY",
                "wire_api": "responses",
            }
        },
    }
    assert requests[0] == (
        "thread/start",
        {
            "cwd": "/workspace",
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "environments": [
                {
                    "environmentId": "session",
                    "cwd": "/workspace",
                    "runtimeWorkspaceRoots": ["/workspace"],
                }
            ],
            "runtimeWorkspaceRoots": ["/workspace"],
            "config": expected_config,
            "modelProvider": "efferva_proxy",
            "model": "gpt-5.4",
        },
    )
    assert requests[1] == (
        "thread/resume",
        {
            "threadId": "codex-thread",
            "cwd": "/workspace",
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "runtimeWorkspaceRoots": ["/workspace"],
            "config": expected_config,
            "modelProvider": "efferva_proxy",
            "model": "gpt-5.4",
        },
    )


@pytest.mark.asyncio
async def test_environment_is_connected_before_it_is_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = CodexRuntime(Path("/not-started"), "postgresql://unused")
    sandbox = sandbox_environment()
    requests: list[tuple[str, dict[str, Any] | None, float]] = []

    async def request(
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_timeout: float = 120,
    ) -> dict[str, Any]:
        requests.append((method, params, request_timeout))
        return {}

    monkeypatch.setattr(runtime, "request", request)

    await runtime.ensure_environment(sandbox)
    await runtime.ensure_environment(sandbox)

    assert [method for method, _, _ in requests] == [
        "environment/add",
        "environment/info",
    ]
    assert requests[1] == (
        "environment/info",
        {"environmentId": "session"},
        5,
    )


@pytest.mark.asyncio
async def test_environment_connection_retries_until_sandbox_is_listening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = CodexRuntime(Path("/not-started"), "postgresql://unused")
    sandbox = sandbox_environment()
    info_attempts = 0
    add_attempts = 0

    async def request(
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_timeout: float = 120,
    ) -> dict[str, Any]:
        nonlocal add_attempts, info_attempts
        if method == "environment/add":
            add_attempts += 1
        if method == "environment/info":
            info_attempts += 1
            if info_attempts == 1:
                raise CodexRpcError(method, {"message": "connection refused"})
        return {}

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(runtime, "request", request)
    monkeypatch.setattr("efferva.runtime.asyncio.sleep", no_sleep)

    await runtime.ensure_environment(sandbox)
    await runtime.ensure_environment(sandbox)

    assert add_attempts == 2
    assert info_attempts == 2


@pytest.mark.asyncio
async def test_runtime_registers_and_executes_an_application_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocations: list[tuple[ToolContext, dict[str, Any]]] = []

    async def calculate(
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> dict[str, int]:
        invocations.append((context, arguments))
        return {"total": int(arguments["left"]) + int(arguments["right"])}

    runtime = CodexRuntime(
        Path("/not-started"),
        "postgresql://unused",
        tools=[
            Tool(
                name="add_numbers",
                description="Add two integers.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "left": {"type": "integer"},
                        "right": {"type": "integer"},
                    },
                    "required": ["left", "right"],
                    "additionalProperties": False,
                },
                handler=calculate,
            )
        ],
    )
    sandbox = sandbox_environment()
    requests: list[tuple[str, dict[str, Any] | None]] = []
    writes: list[dict[str, Any]] = []

    async def request(
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_timeout: float = 120,
    ) -> dict[str, Any]:
        requests.append((method, params))
        return {"thread": {"id": "codex-thread"}}

    async def write(message: dict[str, Any]) -> None:
        writes.append(message)

    monkeypatch.setattr(runtime, "request", request)
    monkeypatch.setattr(runtime, "_write", write)

    assert await runtime.start_thread(sandbox) == "codex-thread"
    run_id = uuid4()
    app_thread_id = uuid4()
    session_id = uuid4()
    runtime.bind_run_context(
        "codex-thread",
        {
            "id": run_id,
            "thread_id": app_thread_id,
            "session_id": session_id,
            "tenant_id": "tenant",
            "owner_issuer": "issuer",
            "owner_subject": "alice",
        },
    )
    assert requests[0][1] is not None
    assert requests[0][1]["dynamicTools"] == [
        {
            "type": "function",
            "name": "add_numbers",
            "description": "Add two integers.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "left": {"type": "integer"},
                    "right": {"type": "integer"},
                },
                "required": ["left", "right"],
                "additionalProperties": False,
            },
            "deferLoading": False,
        }
    ]

    await runtime._handle_server_request(
        {
            "id": 41,
            "method": "item/tool/call",
            "params": {
                "threadId": "codex-thread",
                "turnId": "turn-1",
                "callId": "call-1",
                "namespace": None,
                "tool": "add_numbers",
                "arguments": {"left": 20, "right": 22},
            },
        }
    )

    assert len(invocations) == 1
    context, arguments = invocations[0]
    assert context.thread_id == "codex-thread"
    assert context.turn_id == "turn-1"
    assert context.call_id == "call-1"
    assert context.sandbox is sandbox
    assert context.run_id == run_id
    assert context.app_thread_id == app_thread_id
    assert context.session_id == session_id
    assert context.tenant_id == "tenant"
    assert context.owner_subject == "alice"
    assert arguments == {"left": 20, "right": 22}
    assert writes == [
        {
            "id": 41,
            "result": {
                "contentItems": [{"type": "inputText", "text": '{"total":42}'}],
                "success": True,
            },
        }
    ]


@pytest.mark.asyncio
async def test_runtime_selects_sandbox_skill_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = CodexRuntime(
        Path("/not-started"),
        "postgresql://unused",
        developer_instructions="Product policy.",
        skill_roots=[
            SkillRoot(id="defaults", path="/opt/product/skills"),
            SkillRoot(id="custom", path="/workspace/.agents/skills", enabled_by_default=False),
        ],
    )
    requests: list[tuple[str, dict[str, Any] | None]] = []

    async def request(
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_timeout: float = 120,
    ) -> dict[str, Any]:
        requests.append((method, params))
        return {"thread": {"id": "codex-thread"}}

    monkeypatch.setattr(runtime, "request", request)

    await runtime.start_thread(
        sandbox_environment(),
        {"skill_roots": ["custom"], "reasoning_effort": "high"},
    )

    params = requests[0][1]
    assert params is not None
    assert params["developerInstructions"] == "Product policy."
    assert params["reasoningEffort"] == "high"
    assert params["selectedCapabilityRoots"] == [
        {
            "id": "custom",
            "location": {
                "type": "environment",
                "environmentId": "session",
                "path": "/workspace/.agents/skills",
            },
        }
    ]
