# Efferva

Efferva 是可嵌入产品 FastAPI 应用的多租户云端 Agent 框架。产品继续拥有登录、用户和
组织体系；Efferva 负责身份作用域内的 Session、Thread、Run、Codex Runtime、沙盒、
AG-UI、持久事件、多实例执行租约和基础 WebUI。

## 产品接入

安装当前平台的 Wheel 和 OpenSandbox Provider：

```bash
pip install "efferva[opensandbox]"
```

提供运行环境：

```bash
export EFFERVA_DATABASE_URL=postgresql://user:password@postgres/efferva
export EFFERVA_SANDBOX_PROVIDER=opensandbox
export EFFERVA_OPENSANDBOX_SERVER_URL=http://opensandbox-server:8090
export EFFERVA_OPENSANDBOX_API_KEY=...
export OPENAI_API_KEY=...
# 使用 OpenAI-compatible Responses API 代理时可选：
export EFFERVA_CODEX_OPENAI_BASE_URL=https://llm-proxy.example.com/v1
export EFFERVA_CODEX_MODEL=gpt-5.4
```

安装到现有产品：

```python
from fastapi import FastAPI, Request
from efferva import Efferva, Capability, Principal


async def resolve_principal(request: Request) -> Principal:
    user = request.state.user
    return Principal(
        tenant_id=str(user.organization_id or "default"),
        issuer="my-product",
        subject=str(user.id),
        capabilities=frozenset({Capability.SESSIONS_READ_TENANT}) if user.is_admin else frozenset(),
    )


app = FastAPI()
Efferva(identity=resolve_principal).install(app, prefix="/agent")
```

需要使用 Codex 原生配置时，可以直接传入可信的配置映射：

```python
Efferva(
    identity=resolve_principal,
    codex_config={
        "model_reasoning_effort": "high",
        "web_search": "live",
        "features": {"unified_exec": True},
    },
).install(app, prefix="/agent")
```

部署管理员也可以提供 TOML 文件：

```bash
export EFFERVA_CODEX_CONFIG_FILE=/etc/efferva/codex.toml
```

文件配置与 Python 配置会深度合并，Python 显式配置优先。Efferva 在
`thread/start` 和 `thread/resume` 时把合并结果作为线程配置层注入 Codex App Server；
不要求在共享 `CODEX_HOME` 中维护基础 `config.toml`。模型 Base URL、模型名称及 Efferva
管理的运行环境和安全参数仍由对应的类型化配置覆盖。

需要把 stdio MCP 启动在每个 Session 的远程沙箱时，可在配置值中使用：

- `$EFFERVA_SANDBOX_ENVIRONMENT_ID`：当前 Session 的 Codex environment id；
- `$EFFERVA_SANDBOX_WORKSPACE_PATH`：当前 Session 的 workspace 绝对路径。

Efferva 会在每次 `thread/start` 和 `thread/resume` 注入真实值。例如：

```python
codex_config = {
    "mcp_servers": {
        "domain_tools": {
            "command": "domain-mcp",
            "environment_id": "$EFFERVA_SANDBOX_ENVIRONMENT_ID",
            "cwd": "$EFFERVA_SANDBOX_WORKSPACE_PATH",
        }
    }
}
```

应用内的 Python 能力不需要包装成 MCP Server，可以注册为 Codex App Server 原生
`dynamicTools`：

```python
from efferva import Tool, ToolContext


async def add_numbers(_: ToolContext, arguments):
    return {"total": arguments["left"] + arguments["right"]}


add_tool = Tool(
    name="add_numbers",
    description="Add two numbers.",
    input_schema={
        "type": "object",
        "properties": {
            "left": {"type": "number"},
            "right": {"type": "number"},
        },
        "required": ["left", "right"],
        "additionalProperties": False,
    },
    handler=add_numbers,
)

Efferva(
    identity=resolve_principal,
    tools=[add_tool],
).install(app, prefix="/agent")
```

Efferva 在 `thread/start` 发送 Tool schema，收到 `item/tool/call` server request 后在 App
进程执行 handler，并把结果交回 Codex 继续推理。`ToolContext` 中的 Tenant、Session、
Efferva Thread/Run、Codex Thread/Turn/Call 和 Sandbox 信息由 Efferva 提供，不能由模型
参数覆盖。`dynamicTools` 当前属于 Codex App Server 实验 API。

Workflow 也走同一条 Dynamic Tools 链路，不要求修改或重新编译 Codex：

```python
from efferva import Workflow, workflow_tool


async def research_workflow(context, inputs):
    return await my_dag.run(
        tenant_id=context.tenant_id,
        session_id=context.session_id,
        topic=inputs["topic"],
    )


run_workflow = workflow_tool([
    Workflow(
        name="research",
        description="Run the product research DAG.",
        input_schema={
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
        },
        handler=research_workflow,
    )
])
```

`workflow_tool()` 对 Codex 暴露统一的 `run_workflow`；workflow 名称、DAG 状态、worker
选择和产物策略仍由应用控制。

沙箱镜像里的默认 Skills 和用户写入 Session 工作区的 Skills，可以注册为 Session
本地 Skill Roots：

```python
from efferva import SkillRoot

Efferva(
    identity=resolve_principal,
    skill_roots=[
        SkillRoot(id="product-defaults", path="/opt/product/skills"),
        SkillRoot(id="session-custom", path="/session/workspace/.agents/skills"),
    ],
).install(app, prefix="/agent")
```

Efferva 在 Session sandbox 内让 Codex 扫描 `SKILL.md`。模型、model provider 和推理强度
可设在 Thread，模型及推理强度还可在每次 Run 覆盖。

Codex 原生 Goal 已映射为 Thread API，取消请求是 PostgreSQL 中的持久控制信号，由持有
Run lease 的 worker 执行 `turn/interrupt`。每个 Session 都有独立且持久化的
`CODEX_HOME`，因此启用 Codex 原生 Memory 不会跨 Session 共享状态；产品仍可通过
`native_memory_enabled=False` 全局关闭。

Efferva 还会自动注册 `publish_artifact` Dynamic Tool。Agent 只能发布当前 Session
workspace 内的普通文件；读取前后都会校验 sandbox fencing token。文件内容、哈希和元数据
持久化到 PostgreSQL（默认单文件上限 10 MiB），所以下载不依赖原 worker 或存活中的沙箱。
生产环境后续可将同一 Repository 契约替换为对象存储。

没有现有 FastAPI 宿主时：

```python
app = Efferva(identity=resolve_principal).asgi_app()
```

`install()` 复用宿主应用的中间件和登录态，并在应用生命周期内自动：

1. 建立 PostgreSQL 连接池；
2. 使用 advisory lock 并发安全地执行包内迁移；
3. 定位 Wheel 内的 Codex CLI，供 Session 启动时注入 sandbox；
4. 启动 Run Worker 与空闲 sandbox 回收器；
5. 关闭时按依赖顺序释放资源。

迁移或 Runtime 启动失败时，应用不会进入 Ready。

## 安装包边界

平台 Wheel 包含 Python SDK、FastAPI Router、WebUI、Worker、Repository、迁移、AG-UI、
OpenSandbox Provider 和当前平台的
`efferva/bin/efferva-codex-runtime`。

最终用户不需要：

- Clone `codex`；
- 安装 Rust 或运行 Cargo；
- 设置 `EFFERVA_RUNTIME_BINARY`；
- 单独部署 Efferva 控制面；
- 手动执行数据库迁移；
- 理解 Codex app-server 或沙盒内部进程。

`EFFERVA_RUNTIME_BINARY` 仍是高级诊断覆盖项，不属于正常接入路径。
Runtime 定位不做首次启动联网下载：显式高级覆盖优先，其次使用当前 Wheel 内置二进制；平台
Wheel 缺失时启动会返回包含 OS/架构的明确错误。

## 运行模型

每个产品应用实例就是一个 Efferva 控制面实例。多个相同产品实例通过 PostgreSQL 队列、
租约、fencing token 和持久事件协作，不要求粘性 Session。

- Session 保存 `tenant_id + owner_issuer + owner_subject`；
- Thread、Run、Message 和 Event 通过 Session 继承授权边界；
- 普通用户默认只能访问自己的 Session；
- 租户读、写能力由产品角色映射到 `Capability`，永远不能跨租户；
- 越权子资源返回 `404`，未认证返回 `401`；
- 浏览器断线不取消 Run，`Last-Event-ID` 可从任意实例补流；
- 一个 Session 的多个 Thread 可并行并共享同一工作区；
- 同一 Thread 的 Run 串行；
- 工作区由 OpenSandbox 管理，并通过 Provider 状态与 Session 绑定。

详细一致性语义见 [架构文档](docs/architecture.md)。

## Basic Local Docker

[`examples/basic-local-docker`](examples/basic-local-docker) 是一个独立的接入方项目。它拥有
自己的 `pyproject.toml`，直接声明 Efferva、FastAPI 和 Uvicorn；本地基础设施由该目录的
Compose 文件启动：

- 接入方 FastAPI App；
- PostgreSQL；
- OpenSandbox Server；
- OpenSandbox Docker runtime 创建的每 Session 沙箱和持久工作区。

```bash
OPENAI_API_KEY=... make docker-up
```

打开 <http://localhost:8080>。产品代码只使用固定的本地开发身份并安装 Efferva；沙箱
生命周期、命令和文件访问全部经 OpenSandbox provider，不由接入方直接操作 Docker。

## Provider SDK

内置 `opensandbox` Provider。高级用户可以在进程启动前注册第三方 Provider：

```python
from efferva import Efferva

Efferva.register_sandbox_provider("company-sandbox", CompanySandboxProvider)
```

再通过环境选择：

```bash
export EFFERVA_SANDBOX_PROVIDER=company-sandbox
```

Provider 必须满足统一契约：

```bash
uv run python -m efferva.sandbox.conformance_cli --provider opensandbox
```

契约覆盖工作区和沙盒幂等、流式执行、stdin、并发进程、PTY、终止、文件 API 与
stop/start 持久性。详细接口见 [Provider 文档](docs/sandbox-providers.md)。

## 框架维护与发布

以下命令只面向 Efferva 维护者，不是产品接入步骤：

```bash
make wheel
```

源码工作区保持两个相邻仓库：

```text
codex-cloud-framwork/
├── codex-fork/       # 仅发布构建和上游同步需要
└── agent-framework/  # SDK、控制面、Provider、构建和交付
```

平台 Wheel 的构建矩阵、版本追踪和内部 Registry 发布见
[发布文档](docs/releasing.md)；Codex fork 的维护边界见
[fork 文档](docs/codex-fork.md)。

## HTTP 与 AG-UI

主要接口：

- `GET /api/me`
- `POST /api/sessions`
- `GET /api/sessions?scope=mine|tenant`
- `POST /api/sessions/{session_id}/threads`
- `GET /api/threads/{thread_id}`
- `POST /api/threads/{thread_id}/runs`
- `PUT|GET|DELETE /api/threads/{thread_id}/goal`
- `POST /api/runs/{run_id}/cancel`
- `GET /api/runs/{run_id}/artifacts`
- `GET /api/artifacts/{artifact_id}/content`
- `GET /api/runs/{run_id}/events/stream`
- `POST /api/ag-ui`

AG-UI `runId` 在 Thread 内唯一，可安全重试。事件先写 PostgreSQL，再输出带 SSE `id` 的
AG-UI 流；重连使用 `Last-Event-ID`，普通 Run 流也可使用 `?after=<seq>`。
