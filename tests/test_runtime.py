from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from efferva.runtime import CodexRpcError, CodexRuntime
from efferva.sandbox import SandboxEnvironment, SandboxHandle


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
        "features": {"unified_exec": True},
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
