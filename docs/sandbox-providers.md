# Sandbox Provider SDK

## 稳定边界

Efferva 的 Sandbox 扩展点只有两层：

```text
SandboxProvider
  ├── open()
  ├── ensure(context) -> SandboxEnvironment
  └── close()

SandboxRuntime
  ├── run_command(...)
  ├── read_file(...) / write_file(...)
  ├── list_directory(...) / stat(...)
  └── get_endpoint(port)
```

Provider 负责厂商资源的创建、恢复和连接，Runtime 负责已启动 Sandbox 内的命令、文件与
端口语义。两者不依赖 FastAPI、Session 授权或 PostgreSQL。

## 连接配置与创建决策

Efferva 不读取环境变量、`.env` 或 Secret Manager。产品应用决定配置来源，再把普通配置
对象传给框架和 Provider：

```python
from efferva import CodexConfig, Efferva, EffervaConfig, SandboxLayout
from efferva.sandbox.providers.opensandbox import (
    OpenSandboxConnectionConfig,
    OpenSandboxCreateSpec,
    OpenSandboxProvider,
)

layout = SandboxLayout()
config = EffervaConfig(
    database_url=database_url,
    sandbox=layout,
    codex=CodexConfig(
        api_key=openai_api_key,
        openai_base_url=openai_base_url,
    ),
)
connection = OpenSandboxConnectionConfig(
    server_url=opensandbox_server_url,
    api_key=opensandbox_api_key,
)

async def resolve_sandbox_spec(context):
    account = await billing.get_account(
        tenant_id=context.session.tenant_id,
        user_id=context.session.owner_subject,
    )
    if len(context.active_sandboxes) >= account.max_active_sandboxes:
        raise SandboxQuotaExceeded()
    if account.plan == "pro" and account.remaining_compute > 0:
        return OpenSandboxCreateSpec(
            image="my-product/coding-sandbox:latest",
            cpu_limit="4",
            memory_limit="8Gi",
        )
    return OpenSandboxCreateSpec(
        image="my-product/coding-sandbox:latest",
        cpu_limit="1",
        memory_limit="2Gi",
    )

sandbox = OpenSandboxProvider(
    connection,
    layout=layout,
    resolve_spec=resolve_sandbox_spec,
)

Efferva(
    config=config,
    identity=resolve_principal,
    sandbox=sandbox,
).install(app, prefix="/agent")
```

`context.session` 提供当前 Session 的稳定身份、运行目录以及 `owner_sessions`；
`context.active_sandboxes` 是同一 Owner 当前仍在运行或暂停的 OpenSandbox 库存。库存包含
Session、状态、镜像和创建时资源规格，但不暴露 Credential Proxy 的 bearer token。
产品 resolver 可以结合这些信息查询自己的数据库或计费服务，再返回 OpenSandbox 原生的
创建规格。

resolver 只在没有现成 Sandbox、确实需要创建时执行；恢复或复用已有 Sandbox 不会重新
决策。框架以 Tenant + Owner 为键持有 PostgreSQL advisory lock，因此同一用户的两次
Sandbox 创建不会同时读取到相同库存。产品仍然拥有套餐和计费语义。

如果产品选择环境变量，读取逻辑属于自己的启动层；完整例子见
[`examples/multi-session-codex`](../examples/multi-session-codex)。

## 最低能力

Provider 必须声明 `SandboxCapabilities`。Coding Agent 要求：

- 持久 Session Volume；
- 端口连接；
- 文件操作。

暂停恢复和网络策略是独立能力。控制面启动时会拒绝不满足最低能力的 Provider。

## Credential Proxy

Credential Proxy 也是显式配置，不会从 `OPENAI_API_KEY` 或 Base URL 隐式推导：

```python
from efferva.sandbox.providers.opensandbox import OpenSandboxCredentialProxy

proxy = OpenSandboxCredentialProxy(
    bearer_token=openai_api_key,
    scheme="https",
    host="api.openai.com",
)
```

产品把它放进 resolver 返回的 `OpenSandboxCreateSpec(credential_proxy=proxy)`；传 `None`
就是不启用。这样不同用户也可以使用不同的凭证策略。

## Codex 适配

Codex 不直接依赖 OpenSandbox。Efferva 通过 `SandboxRuntime` 下载并校验固定版本的官方
Codex CLI，然后在每个 Session Sandbox 内启动 app-server：

```text
Efferva App
  -> SandboxRuntime
  -> Session Sandbox / Codex app-server
  -> persistent workspace + CODEX_HOME
```

Docker 或 Kubernetes 是 OpenSandbox Server 的 runtime 选择，不是 Efferva 内的另一套
Provider。
