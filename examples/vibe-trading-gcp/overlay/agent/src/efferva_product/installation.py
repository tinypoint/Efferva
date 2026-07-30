"""Install Efferva into the existing Vibe-Trading FastAPI product."""

from __future__ import annotations

from fastapi import FastAPI
from src.efferva_product.compat_routes import register_efferva_compat_routes
from src.efferva_product.identity import resolve_principal
from src.efferva_product.tools import POSITION_SIZE_TOOL, RUN_WORKFLOW_TOOL

from efferva import Efferva, SkillRoot

_DEVELOPER_INSTRUCTIONS = """\
You are the coding and research agent inside Vibe-Trading.
Use the sandbox workspace for files and reproducible analysis.
Use native Codex skills when a matching skill is available.
For complex research that benefits from Vibe-Trading's preset multi-agent DAGs, call
run_workflow with workflow="vibe_research". Publish user-facing reports and charts with
publish_artifact. Do not claim that a trade was placed unless a separately approved
live-trading tool confirms it.
"""


def install_efferva_product(app: FastAPI) -> None:
    """Use Efferva for chat control-plane state while retaining Vibe's product UI."""
    Efferva(
        identity=resolve_principal,
        tools=[POSITION_SIZE_TOOL, RUN_WORKFLOW_TOOL],
        developer_instructions=_DEVELOPER_INSTRUCTIONS,
        skill_roots=[
            SkillRoot(
                id="vibe-trading-defaults",
                path="/app/agent/src/skills",
            ),
            SkillRoot(
                id="session-custom",
                path="/workspace/.agents/skills",
            ),
        ],
        codex_config={
            "model_reasoning_effort": "high",
        },
    ).install(app, prefix="/efferva")
    register_efferva_compat_routes(app)
