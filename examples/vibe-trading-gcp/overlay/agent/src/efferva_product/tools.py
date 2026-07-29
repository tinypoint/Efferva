from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from efferva import Tool, ToolContext


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
