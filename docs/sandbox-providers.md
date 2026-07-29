# Sandbox Provider SDK

## 稳定边界

Efferva 的 Sandbox 扩展点分成生命周期与运行时两层：

```text
SandboxProvider
  ├── ensure_workspace(context) -> WorkspaceHandle
  ├── start(context, workspace) -> SandboxHandle
  ├── connect(sandbox) -> SandboxRuntime
  ├── stop(sandbox)
  └── destroy(sandbox)

SandboxRuntime
  ├── process: start / read(cursor) / stdin / resize / terminate
  └── filesystem: read / write / list / stat
```

Provider 负责厂商资源的创建和连接，Runtime 负责一个已启动沙盒内的执行语义。两者都不依赖
Codex、FastAPI、Session 授权或 PostgreSQL。

`WorkspaceHandle.external_ref/state` 与 `SandboxHandle.external_ref/state` 是 Provider 的
不透明状态。Efferva 只持久化和原样交还，不从中解析 Docker、Kubernetes 或厂商字段。

## 最低能力

Provider 必须声明 `SandboxCapabilities`。Coding Agent 控制面要求：

- 流式进程输出与稳定 cursor；
- stdin；
- 进程终止；
- 文件读写；
- 持久工作区；
- 并发进程。

PTY、快照、暂停恢复、端口转发和网络策略是独立能力，不允许尚未实现的 Provider 声明为
`True`。控制面在启动时拒绝不满足最低能力的 Provider。

## 注册与配置

第三方包可在应用启动前注册零参数 Provider factory 或类：

```python
from efferva import Efferva

Efferva.register_sandbox_provider(
    "company-sandbox",
    CompanySandboxProvider,
)
```

部署环境再选择实现：

```bash
EFFERVA_SANDBOX_PROVIDER=company-sandbox
```

`opensandbox` 是保留的一方名称。自定义 Provider 不应要求产品把 API key 或厂商参数写入
`Efferva(...)`；它应从自己的部署配置读取。

OpenSandbox Provider 通过官方 Python SDK 连接 OpenSandbox Server。基础本地示例让
OpenSandbox Server 使用 Docker runtime：

```bash
pip install "efferva[opensandbox]"
export EFFERVA_SANDBOX_PROVIDER=opensandbox
export EFFERVA_OPENSANDBOX_SERVER_URL=http://localhost:8090
export EFFERVA_OPENSANDBOX_API_KEY=...
```

完整 Compose 见
[`examples/basic-local-docker`](../examples/basic-local-docker)。

## Codex 适配

Codex 不直接依赖 Provider。每个 Efferva 实例中的 Executor Gateway 实现 Codex 已有的
远程 exec-server WebSocket 协议，并把请求转换到 `SandboxRuntime`：

```text
Codex Runtime
  -> loopback Executor Gateway
  -> SandboxRuntime
  -> OpenSandbox execd API / third-party API
```

Gateway endpoint 和随机 token 只存在于当前进程内存中，不写入数据库。每个请求都校验当前
Sandbox lease 的 fencing token，过期实例无法继续执行。模型 Base URL 与 API key 留在
Efferva 实例，永远不传入 Sandbox。

当前 Gateway 覆盖 Codex Coding Agent 使用的进程与文件协议。远程环境的通用 HTTP proxy
能力不是 MVP 契约；未来如需支持依赖该能力的外部 MCP，应作为显式 capability 扩展，而不是
让 Provider 猜测 Codex 协议。

## Conformance

共享认证入口：

```bash
uv run python -m efferva.sandbox.conformance_cli --provider opensandbox
```

Python 适配包也可以直接调用 `run_provider_conformance(provider)`。认证检查：

1. Workspace 与 Sandbox 幂等；
2. capability negotiation；
3. 文件写入、读取、目录列表和 metadata；
4. stdout/stderr 流式执行；
5. stdin；
6. 并发进程；
7. PTY（声明支持时）；
8. 进程组终止；
9. stop/start 后 Workspace 持久。

只有通过这套认证和同一套 Codex E2E 的实现，才应声明 Efferva Coding Agent 兼容。

## 第一方 Provider

| Provider | 执行通道 | Workspace | Sandbox 内组件 |
|---|---|---|---|
| OpenSandbox | OpenSandbox execd API | 由 OpenSandbox runtime 管理 | OpenSandbox execd |

Docker 或 Kubernetes 是 OpenSandbox Server 的部署/runtime 选择，不是 Efferva 内的另一套
Provider。E2B 等外部 Provider 应新增独立适配包并复用本契约，不修改控制面核心。
