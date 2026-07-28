from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from agentframe.config import Settings
from agentframe.sandbox.conformance import run_provider_conformance
from agentframe.sandbox.manager import create_sandbox_provider


async def main(provider_name: str | None) -> None:
    settings = Settings(
        **({"sandbox_provider": provider_name} if provider_name is not None else {})
    )
    provider = create_sandbox_provider(settings)
    try:
        report = await run_provider_conformance(provider)
        print(json.dumps(asdict(report), separators=(",", ":")))
    finally:
        close = getattr(provider, "close", None)
        if close is not None:
            await close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider")
    arguments = parser.parse_args()
    asyncio.run(main(arguments.provider))
