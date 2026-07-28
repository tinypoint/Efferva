---
title: 快速开始
description: 预览 Efferva 最小化的 FastAPI 接入边界。
sidebar:
  order: 2
  label: 快速开始
---

Efferva 目前处于内部预览阶段。各平台 Wheel 和完整安装指南将在首次公开发布前提供。

## 接入形态

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

身份解析器是唯一必需的产品接入边界。Efferva 会复用宿主应用的生命周期和中间件。

:::caution[预览契约]
目前不要把本页当成稳定安装契约。v0.1 的打包与发布流程仍在定稿。
:::

下一步了解 [Session、Thread 与 Run](concepts/) 的关系。
