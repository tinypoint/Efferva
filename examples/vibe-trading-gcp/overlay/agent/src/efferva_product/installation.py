"""Install Efferva into the existing Vibe-Trading FastAPI product."""

from __future__ import annotations

from fastapi import FastAPI
from src.efferva_product.compat_routes import register_efferva_compat_routes
from src.efferva_product.identity import resolve_principal
from src.efferva_product.tools import POSITION_SIZE_TOOL

from efferva import Efferva


def install_efferva_product(app: FastAPI) -> None:
    """Use Efferva for chat control-plane state while retaining Vibe's product UI."""
    Efferva(
        identity=resolve_principal,
        tools=[POSITION_SIZE_TOOL],
        codex_config={
            "model_reasoning_effort": "high",
        },
    ).install(app, prefix="/efferva")
    register_efferva_compat_routes(app)
