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

from efferva import Efferva, Principal


async def resolve_principal(request: Request) -> Principal:
    user = request.state.user
    return Principal(
        tenant_id=str(user.organization_id),
        issuer="my-product",
        subject=str(user.id),
    )


app = FastAPI()
Efferva(identity=resolve_principal).install(app, prefix="/agent")
```

The identity resolver is the only required product-specific boundary. Efferva reuses the host application lifecycle and middleware.

:::caution[Preview contract]
Do not treat this page as a stable installation contract yet. Packaging and release instructions are being finalized for v0.1.
:::

Next, learn how [sessions, threads, and runs](concepts/) relate.
