---
title: Getting started
description: Preview the minimal FastAPI integration boundary for Efferva.
sidebar:
  order: 2
  label: Getting started
---

Efferva is currently in private preview. The Python wheel and complete installation guide will be published before the first public release.

## Integration shape

```python
from fastapi import FastAPI, Request

from efferva import Codex, Efferva, EffervaConfig, Principal, SandboxLayout
from efferva.sandbox.providers.opensandbox import (
    OpenSandboxConnectionConfig,
    OpenSandboxCreateSpec,
    OpenSandboxProvider,
)


async def resolve_principal(request: Request) -> Principal:
    user = request.state.user
    return Principal(
        tenant_id=str(user.organization_id),
        issuer="my-product",
        subject=str(user.id),
    )


async def resolve_sandbox_spec(context) -> OpenSandboxCreateSpec:
    plan = await billing.plan_for(
        context.session.tenant_id,
        context.session.owner_subject,
    )
    if len(context.active_sandboxes) >= plan.max_active_sandboxes:
        raise SandboxQuotaExceeded()
    if plan.name == "pro":
        return OpenSandboxCreateSpec(image="product/sandbox", cpu_limit="4")
    return OpenSandboxCreateSpec(image="product/sandbox", cpu_limit="1")


app = FastAPI()
layout = SandboxLayout()
Efferva(
    config=EffervaConfig(
        database_url=database_url,
        sandbox=layout,
    ),
    identity=resolve_principal,
    sandbox=OpenSandboxProvider(
        OpenSandboxConnectionConfig(server_url=opensandbox_server_url),
        layout=layout,
        resolve_spec=resolve_sandbox_spec,
    ),
    engine=Codex(api_key=openai_api_key),
).install(app, prefix="/agent")
```

The product owns identity, configuration sources, and per-session sandbox policy. Efferva receives explicit values and never reads environment variables or `.env` files. The sandbox resolver receives the owner's Sessions and active Sandboxes, and can combine that inventory with current plan or quota data to choose an image and resources.

:::caution[Preview contract]
Do not treat this page as a stable installation contract yet. Packaging and release instructions are being finalized for v0.1.
:::

Next, learn how [sessions, threads, and runs](concepts/) relate.
