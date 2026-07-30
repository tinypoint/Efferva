from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from efferva import Tool, ToolContext, Workflow, workflow_tool


async def calculate_position_size(
    _: ToolContext,
    arguments: Mapping[str, Any],
) -> dict[str, str | int]:
    """Calculate a risk-limited position size without placing an order."""
    try:
        account_value = Decimal(str(arguments["account_value"]))
        risk_percent = Decimal(str(arguments["risk_percent"]))
        entry_price = Decimal(str(arguments["entry_price"]))
        stop_price = Decimal(str(arguments["stop_price"]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ValueError(
            "account_value, risk_percent, entry_price and stop_price are required"
        ) from error

    if account_value <= 0:
        raise ValueError("account_value must be positive")
    if risk_percent <= 0 or risk_percent > 100:
        raise ValueError("risk_percent must be between 0 and 100")
    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share == 0:
        raise ValueError("entry_price and stop_price must be different")

    risk_budget = account_value * risk_percent / Decimal("100")
    shares = int(risk_budget // risk_per_share)
    position_value = Decimal(shares) * entry_price
    return {
        "shares": shares,
        "risk_budget": format(risk_budget.quantize(Decimal("0.01")), "f"),
        "risk_per_share": format(risk_per_share.quantize(Decimal("0.01")), "f"),
        "position_value": format(position_value.quantize(Decimal("0.01")), "f"),
        "note": "Calculation only; no trade was placed.",
    }


POSITION_SIZE_TOOL = Tool(
    name="calculate_position_size",
    description=(
        "Calculate the maximum whole-share position size from account value, risk percentage, "
        "entry price, and stop price. This tool never places an order."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "account_value": {
                "type": "number",
                "description": "Total account value in the account currency.",
                "exclusiveMinimum": 0,
            },
            "risk_percent": {
                "type": "number",
                "description": "Maximum percentage of account value to risk.",
                "exclusiveMinimum": 0,
                "maximum": 100,
            },
            "entry_price": {
                "type": "number",
                "description": "Planned entry price per share.",
            },
            "stop_price": {
                "type": "number",
                "description": "Planned stop-loss price per share.",
            },
        },
        "required": [
            "account_value",
            "risk_percent",
            "entry_price",
            "stop_price",
        ],
        "additionalProperties": False,
    },
    handler=calculate_position_size,
)


async def run_vibe_research_workflow(
    context: ToolContext,
    inputs: Mapping[str, Any],
) -> Any:
    """Execute Vibe-Trading's preset DAG without blocking Codex's RPC reader."""
    prompt = inputs.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt is required")
    preset_name = inputs.get("preset_name")
    if preset_name is not None and not isinstance(preset_name, str):
        raise ValueError("preset_name must be a string")

    from src.tools.swarm_tool import SwarmTool

    tenant_key = hashlib.sha256(
        (context.tenant_id or "unknown").encode("utf-8")
    ).hexdigest()[:24]
    session_key = str(context.session_id or "unknown")
    base_dir = (
        Path("/home/vibe/.vibe-trading/efferva")
        / tenant_key
        / session_key
        / "swarm"
    )
    result = await asyncio.to_thread(
        SwarmTool(include_shell_tools=False, base_dir=base_dir).execute,
        prompt=prompt,
        preset_name=preset_name,
    )
    try:
        return json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return result


VIBE_RESEARCH_WORKFLOW = Workflow(
    name="vibe_research",
    description=(
        "Run one of Vibe-Trading's preset multi-agent DAGs for complex investment research. "
        "The workflow chooses a preset automatically unless preset_name is supplied."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The complete research assignment.",
            },
            "preset_name": {
                "type": "string",
                "description": "Optional Vibe-Trading preset name.",
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
    handler=run_vibe_research_workflow,
)

RUN_WORKFLOW_TOOL = workflow_tool([VIBE_RESEARCH_WORKFLOW])
