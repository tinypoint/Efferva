"""Install Efferva into the existing Vibe-Trading FastAPI product."""

from __future__ import annotations

from fastapi import FastAPI

from efferva import Efferva
from src.efferva_product.compat_routes import register_efferva_compat_routes
from src.efferva_product.identity import resolve_principal


def install_efferva_product(app: FastAPI) -> None:
    """Use Efferva for chat control-plane state while retaining Vibe's product UI."""
    Efferva(
        identity=resolve_principal,
        codex_config={
            "model_reasoning_effort": "high",
            "mcp_servers": {
                "vibe_trading": {
                    "command": "vibe-trading-mcp",
                    "args": [],
                    "cwd": "$EFFERVA_SANDBOX_WORKSPACE_PATH",
                    "environment_id": "$EFFERVA_SANDBOX_ENVIRONMENT_ID",
                    "env_vars": [
                        "LANGCHAIN_PROVIDER",
                        "LANGCHAIN_MODEL_NAME",
                        "OPENAI_API_KEY",
                        "OPENAI_BASE_URL",
                    ],
                    "required": True,
                    "startup_timeout_sec": 120,
                    "tool_timeout_sec": 3600,
                }
            },
        },
    ).install(app, prefix="/efferva")
    register_efferva_compat_routes(app)
