from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from efferva import Workflow, workflow_tool
from efferva.sandbox import SandboxEnvironment, SandboxHandle
from efferva.tools import ToolContext


@pytest.mark.asyncio
async def test_workflow_tool_dispatches_application_workflow() -> None:
    calls: list[dict[str, Any]] = []

    async def handler(_: ToolContext, inputs: dict[str, Any]) -> dict[str, Any]:
        calls.append(inputs)
        return {"status": "completed", "symbol": inputs["symbol"]}

    tool = workflow_tool(
        [
            Workflow(
                name="research",
                description="Research one symbol.",
                input_schema={"type": "object"},
                handler=handler,
            )
        ]
    )
    context = ToolContext(
        thread_id="codex-thread",
        turn_id="turn",
        call_id="call",
        sandbox=SandboxEnvironment(
            environment_id="environment",
            endpoint="ws://executor",
            workspace_path="/workspace",
            sandbox=SandboxHandle(
                provider="test",
                external_ref="sandbox",
                workspace_id=uuid4(),
            ),
        ),
    )

    result = await tool.invoke(
        context,
        {"workflow": "research", "inputs": {"symbol": "AAPL"}},
    )

    assert result == {"status": "completed", "symbol": "AAPL"}
    assert calls == [{"symbol": "AAPL"}]
    assert tool.codex_spec()["name"] == "run_workflow"
