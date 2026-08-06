---
title: 快速开始
description: 预览 Efferva 最小化的 FastAPI 接入边界。
sidebar:
  order: 2
  label: 快速开始
---

Efferva 目前处于内部预览阶段。Python Wheel 和完整安装指南将在首次公开发布前提供。

## 接入形态

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

产品拥有身份、配置来源和逐 Session 的沙箱策略。Efferva 只接收显式值，不读取环境变量或 `.env` 文件。resolver 可以看到该 Owner 的 Sessions 和仍处于活动状态的 Sandboxes，再结合实时套餐和余量决定镜像与资源。

:::caution[预览契约]
目前不要把本页当成稳定安装契约。v0.1 的打包与发布流程仍在定稿。
:::

下一步了解 [Session、Thread 与 Run](concepts/) 的关系。
