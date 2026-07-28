# Efferva

Efferva 是可嵌入产品 FastAPI 应用的多租户云端 Agent 框架。产品继续拥有登录、用户和
组织体系；Efferva 负责身份作用域内的 Session、Thread、Run、Codex Runtime、沙盒、
AG-UI、持久事件、多实例执行租约和基础 WebUI。

## 产品接入

安装当前平台的 Wheel：

```bash
pip install efferva
```

提供运行环境：

```bash
export EFFERVA_DATABASE_URL=postgresql://user:password@postgres/efferva
export EFFERVA_SANDBOX_PROVIDER=docker
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

没有现有 FastAPI 宿主时：

```python
app = Efferva(identity=resolve_principal).asgi_app()
```

`install()` 复用宿主应用的中间件和登录态，并在应用生命周期内自动：

1. 建立 PostgreSQL 连接池；
2. 使用 advisory lock 并发安全地执行包内迁移；
3. 定位并启动 Wheel 内的 Codex Runtime；
4. 启动 loopback Executor Gateway 和 Run Worker；
5. 关闭时按依赖顺序释放资源。

迁移或 Runtime 启动失败时，应用不会进入 Ready。

## 安装包边界

平台 Wheel 包含 Python SDK、FastAPI Router、WebUI、Worker、Repository、迁移、AG-UI、
Executor Gateway、Docker/Kubernetes Provider 和当前平台的
`efferva/bin/efferva-codex-runtime`。

最终用户不需要：

- Clone `codex`；
- 安装 Rust 或运行 Cargo；
- 设置 `EFFERVA_RUNTIME_BINARY`；
- 单独部署 Efferva 控制面；
- 手动执行数据库迁移；
- 理解 Executor Gateway。

`EFFERVA_RUNTIME_BINARY`、Gateway host/port 仍是高级诊断覆盖项，不属于正常接入路径。
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
- Docker 使用每 Session 一个 named volume，Kubernetes 使用每 Session 一个 PVC。

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
make wheel-docker
OPENAI_API_KEY=... docker compose \
  --file examples/basic-local-docker/compose.yaml \
  up --build
```

打开 <http://localhost:8080>。产品代码只使用固定的本地开发身份并安装 Efferva；沙箱
生命周期、命令和文件访问全部经 OpenSandbox provider，不由接入方直接操作 Docker。

## Docker 与 Kind 验收

要求 Docker Desktop；Kind 还要求 `kind`、`kubectl` 和 `jq`。

```bash
export OPENAI_API_KEY=...
make docker-up
```

Compose 会先构建 Linux 平台 Wheel，最终 App 镜像只安装 Wheel，不从源码编译 Python 包或
Codex。MVP 的 Linux Wheel 以 Debian 12 / glibc 2.36 为受控运行基线，不宣称通用
manylinux 兼容。打开 <http://localhost:8080>，停止时执行：

```bash
make docker-down
```

Kind 双实例：

```bash
export OPENAI_API_KEY=...
export EFFERVA_CODEX_OPENAI_BASE_URL=http://host.docker.internal:8317/v1
export EFFERVA_CODEX_MODEL=gpt-5.4
make kind-up
make kind-smoke
make kind-e2e
```

本机已有名为 `cli-proxy-api` 的 CLIProxyAPI 容器时：

```bash
make kind-up-cliproxy
make kind-e2e
```

Kind E2E 验证两个产品 Pod、租户隔离、同 Session 两个并行 Thread、共享 PVC 文件，以及
浏览器从另一实例按 `Last-Event-ID` 补流。

## Provider SDK

内置 `docker` 和 `kubernetes` Provider。高级用户可以在进程启动前注册第三方 Provider：

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
uv run python -m efferva.sandbox.conformance_cli --provider docker
uv run python -m efferva.sandbox.conformance_cli --provider kubernetes
```

契约覆盖工作区和沙盒幂等、流式执行、stdin、并发进程、PTY、终止、文件 API 与
stop/start 持久性。详细接口见 [Provider 文档](docs/sandbox-providers.md)。

## 框架维护与发布

以下命令只面向 Efferva 维护者，不是产品接入步骤：

```bash
uv sync --extra dev
make check
uv run pytest -q
make wheel
make wheel-smoke
make docker-e2e
```

源码工作区保持两个相邻仓库：

```text
agent-framework/
├── codex/       # 仅发布构建和上游同步需要
└── Efferva/      # SDK、控制面、Provider、构建和交付
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
- `GET /api/runs/{run_id}/events/stream`
- `POST /api/ag-ui`

AG-UI `runId` 在 Thread 内唯一，可安全重试。事件先写 PostgreSQL，再输出带 SSE `id` 的
AG-UI 流；重连使用 `Last-Event-ID`，普通 Run 流也可使用 `?after=<seq>`。
