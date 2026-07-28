# AgentFrame

AgentFrame 是一个基于 Codex 的多租户云端 Agent 产品框架。产品继续拥有自己的登录、用户和
组织体系，只需要把当前请求转换成 `Principal`，即可获得按用户隔离的 Session、Thread、
多轮对话、AG-UI 事件流、沙盒工作区、多实例补流和基础 WebUI。

```python
from fastapi import FastAPI, Request
from agentframe import AgentFrame, Capability, Principal


async def resolve_principal(request: Request) -> Principal:
    user = request.state.user  # 产品自己的 Cookie、JWT 或 SSO 中间件
    return Principal(
        tenant_id=str(user.organization_id or "default"),
        issuer="my-product",
        subject=str(user.id),
        capabilities=frozenset(
            {
                Capability.SESSIONS_READ_TENANT,
            }
        )
        if user.is_admin
        else frozenset(),
    )


app = FastAPI(title="My Product")
AgentFrame(identity=resolve_principal).install(app, prefix="/agent")
```

没有现有 FastAPI 宿主时，可以基于相同模块创建完整 ASGI 应用：

```python
app = AgentFrame(identity=resolve_principal).asgi_app()
```

Model、OpenAI-compatible Base URL 与 API Key 由部署环境决定，不是产品代码的必填参数。

MVP 范围是 Docker Compose 与 Kind。GKE 会复用 Kubernetes backend，但不在第一版
验收范围内。

## 已实现的能力

- Codex Runtime 位于沙盒外；命令与文件操作经远程 exec-server 进入沙盒。
- App Session、Thread、Run、Message 与 AG-UI 事件持久化在 PostgreSQL。
- Codex 原生 ThreadStore 通过薄 fork 注入 PostgreSQL，支持跨实例
  `thread/list`、`thread/read`、`thread/resume`。
- 浏览器断线不取消 Run；SSE 使用事件序号与 `Last-Event-ID` 从任意 Web 实例补流。
- 一个 Session 对应一个共享工作区；其中不同 Thread 可并行，同一 Thread 的 Run 串行。
- Docker 使用每 Session 一个 named volume；Kubernetes 使用每 Session 一个 PVC。
- 内置 Session 列表、Thread 列表和对话 WebUI。
- Session 由 `tenant_id + issuer + subject` 标记所有者；所有子资源通过 Session 继承权限。
- 普通用户默认只能看到自己的 Session；产品可以把自己的角色映射为租户级读写能力。

架构与一致性语义见 [docs/architecture.md](docs/architecture.md)，Codex fork 的维护方法见
[docs/codex-fork.md](docs/codex-fork.md)。

## 产品集成示例

[`examples/basic-product`](examples/basic-product) 模拟了一个已经拥有 Cookie 登录体系的产品，
并提供 Alice、Bob、同租户只读管理员和另一租户管理员四个身份：

```bash
uv run uvicorn --app-dir examples/basic-product main:app --reload
```

打开 <http://localhost:8000>。示例登录只用于演示集成边界，不是生产认证实现。生产环境没有
匿名默认身份；身份解析失败应抛出 `UnauthenticatedError`。

框架同时提供可复用运行时基础镜像和薄产品镜像路径：

```bash
make docker-example
```

产品镜像只需继承 `agentframe-runtime:local` 并复制自己的应用代码。

## Docker 验收

要求：Docker Desktop，以及可用的 `OPENAI_API_KEY`。

```bash
export OPENAI_API_KEY=...
make docker-up
```

打开 <http://localhost:8080>。Compose 和 Kind 默认运行打包内置的开发身份适配器，方便本地
体验；它不能用于生产。停止控制面：

```bash
make docker-down
```

Session 对应的 `af-sandbox-*` 容器和 `af-workspace-*` volume 是持久化资源，不会随
`docker compose down` 自动删除。MVP 暂不提供 UI 删除 Session，以避免误删工作区。

## Kind 验收

要求：Docker、Kind、kubectl。

```bash
export OPENAI_API_KEY=...
make kind-up
make kind-smoke
make kind-port-forward
```

使用 OpenAI-compatible Responses API 代理时：

```bash
export OPENAI_API_KEY=代理的_api_key
export AGENTFRAME_CODEX_OPENAI_BASE_URL=http://host.docker.internal:8317/v1
export AGENTFRAME_CODEX_MODEL=gpt-5.4
make kind-up
```

`host.docker.internal` 适用于 Docker Desktop 本地 Kind；部署到 GKE 时应替换为集群可达的
内部 HTTPS 地址。AgentFrame 会为该地址创建 Codex custom provider，并从
`OPENAI_API_KEY` 环境变量读取 Bearer Token。API key 只进入 Kubernetes Secret，不写入
镜像或仓库。

本机运行默认名为 `cli-proxy-api` 的 CLIProxyAPI 容器时，可以直接执行：

```bash
make kind-up-cliproxy
make kind-e2e
```

脚本会从容器的 `/CLIProxyAPI/config.yaml` bind mount 读取首个 `api-keys` 值，并自动发现
宿主机映射端口。可用 `AGENTFRAME_CLIPROXY_CONTAINER` 和 `AGENTFRAME_CODEX_MODEL`
覆盖容器名与模型。

打开 <http://localhost:8080>。`kind-up` 会：

1. 创建一个双节点 `agentframe` Kind 集群；
2. 构建并加载 App 与 Sandbox 镜像；
3. 部署一个 PostgreSQL、两个 AgentFrame App 副本；
4. 安装仅能创建/读取 Pod、Service、PVC 的 namespace 级 RBAC。

`kind-smoke` 不要求真实模型；`kind-e2e` 要求可用模型，并额外验证双 Pod、租户隔离、同一
Session 的两个并行 Thread、共享 PVC 文件，以及从另一 Pod 使用 `Last-Event-ID` 补流。

删除验收集群：

```bash
make kind-down
```

## 本地开发

```bash
uv sync --extra dev
cargo build --workspace
cp .env.example .env
agentframe
```

默认数据库地址在 `.env.example` 中。默认 Docker backend 会调用本机 Docker CLI。

常规检查：

```bash
make check
uv run pytest -q
```

无需真实模型额度的 Docker 端到端验收会启动一个本地 Responses API stub，让 Codex
真实调用统一 `exec_command` 写入远程 Sandbox，再验证持久 Run、AG-UI 回放和工作区文件：

```bash
make docker-e2e
```

PostgreSQL 与 Codex Runtime 集成测试：

```bash
AGENTFRAME_TEST_DATABASE_URL=postgresql://agentframe:agentframe@localhost:5432/agentframe \
  uv run pytest -q -m integration
```

## HTTP 与 AG-UI

主要接口：

- `GET /api/me`
- `POST /api/sessions`
- `GET /api/sessions?scope=mine|tenant`
- `POST /api/sessions/{session_id}/threads`
- `GET /api/threads/{thread_id}`
- `POST /api/threads/{thread_id}/runs`
- `GET /api/runs/{run_id}/events/stream`
- `POST /api/ag-ui`

`POST /api/ag-ui` 接受 AG-UI `RunAgentInput`。传入稳定的 `runId` 可安全重试；相同
Thread 内相同 `runId` 不会创建第二个 Run，不同 Thread 或用户可以复用客户端生成的 ID。
服务返回带 SSE `id` 的 AG-UI 事件。重连时发送
`Last-Event-ID`，或在普通 Run 流接口使用 `?after=<seq>`。

`scope` 默认是 `mine`。只有 Principal 拥有 `SESSIONS_READ_TENANT` 时才可请求
`scope=tenant`；`SESSIONS_WRITE_TENANT` 单独控制对其他所有者 Session 的 Thread/Run 写入。
任何跨租户访问以及无权限的直接资源访问都返回 `404`。

目前映射的标准事件包括：

- `RUN_STARTED`
- `TEXT_MESSAGE_START`
- `TEXT_MESSAGE_CONTENT`
- `TEXT_MESSAGE_END`
- `RUN_FINISHED`
- `RUN_ERROR`
- `RAW`（保留 Codex 的计划、命令、文件变更等原始通知）

## 仓库结构

```text
codex-cloud-framwork/
├── codex-fork/       # tinypoint/codex：ThreadStore 注入点与冷恢复修复
└── agent-framework/  # 产品框架、PostgreSQL Store、WebUI、Docker、Kind
```
