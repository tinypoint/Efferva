from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeAlias

from efferva.tools import Tool, ToolContext

WorkflowHandler: TypeAlias = Callable[
    [ToolContext, Mapping[str, Any]],
    Any | Awaitable[Any],
]


@dataclass(frozen=True, slots=True)
class Workflow:
    """An application-owned workflow exposed through the dynamic tool bridge."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: WorkflowHandler

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Workflow name must not be empty")
        if not self.description.strip():
            raise ValueError(f"Workflow {self.name!r} description must not be empty")
        if self.input_schema.get("type") != "object":
            raise ValueError(f"Workflow {self.name!r} input_schema must describe an object")
        if not callable(self.handler):
            raise TypeError(f"Workflow {self.name!r} handler must be callable")

    async def invoke(self, context: ToolContext, inputs: Mapping[str, Any]) -> Any:
        result = self.handler(context, inputs)
        if inspect.isawaitable(result):
            return await result
        return result


def workflow_tool(
    workflows: Sequence[Workflow],
    *,
    name: str = "run_workflow",
    defer_loading: bool = False,
) -> Tool:
    """Build one Codex dynamic tool that dispatches to application workflows."""

    registry = {workflow.name: workflow for workflow in workflows}
    if not registry:
        raise ValueError("workflow_tool requires at least one Workflow")
    if len(registry) != len(workflows):
        raise ValueError("Workflow names must be unique")

    descriptions = "\n".join(
        f"- {workflow.name}: {workflow.description}" for workflow in registry.values()
    )

    async def run(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> Any:
        workflow_name = arguments.get("workflow")
        if not isinstance(workflow_name, str) or workflow_name not in registry:
            choices = ", ".join(sorted(registry))
            raise ValueError(f"workflow must be one of: {choices}")
        inputs = arguments.get("inputs", {})
        if not isinstance(inputs, Mapping):
            raise ValueError("inputs must be a JSON object")
        return await registry[workflow_name].invoke(context, inputs)

    return Tool(
        name=name,
        description=(
            "Run an application-defined workflow. Workflows are orchestrated by the host "
            "application and may execute a DAG or multiple workers.\n"
            f"Available workflows:\n{descriptions}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "workflow": {
                    "type": "string",
                    "enum": sorted(registry),
                    "description": "Registered workflow name.",
                },
                "inputs": {
                    "type": "object",
                    "description": "Workflow-specific input object.",
                },
            },
            "required": ["workflow", "inputs"],
            "additionalProperties": False,
        },
        handler=run,
        defer_loading=defer_loading,
    )
