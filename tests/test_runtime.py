from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agentframe.runtime import CodexRpcError, CodexRuntime
from agentframe.sandbox import SandboxEnvironment, SandboxHandle


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


def test_runtime_command_uses_codex_provider_overrides() -> None:
    runtime = CodexRuntime(
        Path("/runtime"),
        "postgresql://unused",
        openai_base_url="http://proxy:8317/v1",
        model="gpt-5.4",
    )

    assert runtime._runtime_command() == [
        "/runtime",
        "--config",
        'model_provider="agentframe_proxy"',
        "--config",
        'model_providers.agentframe_proxy.name="AgentFrame LLM proxy"',
        "--config",
        'model_providers.agentframe_proxy.base_url="http://proxy:8317/v1"',
        "--config",
        'model_providers.agentframe_proxy.env_key="OPENAI_API_KEY"',
        "--config",
        'model_providers.agentframe_proxy.wire_api="responses"',
        "--config",
        'model="gpt-5.4"',
    ]


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
    monkeypatch.setattr("agentframe.runtime.asyncio.sleep", no_sleep)

    await runtime.ensure_environment(sandbox)
    await runtime.ensure_environment(sandbox)

    assert add_attempts == 2
    assert info_attempts == 2
