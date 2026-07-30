# Basic Local Docker

这是 Efferva 的第一个接入示例：一个独立的 FastAPI 产品通过 Docker Compose 在本机启动，
并使用 OpenSandbox Server 的 Docker runtime 创建 Agent 沙箱。

这个目录拥有自己的 `pyproject.toml`，直接声明产品代码使用的 Efferva、FastAPI 和
Uvicorn，不依赖 Efferva 仓库的开发环境或传递依赖。

## 架构

```text
Browser
  │
  ▼
basic-local-docker app
  ├── Efferva App / Run Worker
  ├── PostgreSQL
  └── OpenSandbox Python SDK
          │
          ▼
      OpenSandbox Server
          │ Docker runtime
          ▼
      per-Session Codex app-server + persistent Session volume
```

OpenSandbox Server 通过挂载的 Docker socket 创建沙箱；Efferva 不直接操作 Docker
daemon。每个 Session 对应一个 OpenSandbox sandbox，并把持久卷挂载到 `/session`：
工作区为 `/session/workspace`，Codex 原生状态为 `/session/codex-home`。

## 启动

要求：

- Docker Desktop 或 Docker Engine；
- 可用的 `OPENAI_API_KEY`；
- 已通过根目录 `make wheel` 构建 Efferva 纯 Python Wheel。

在仓库根目录执行：

```bash
OPENAI_API_KEY=... make docker-up
```

`make wheel` 只构建 Efferva Python 包；产品启动时下载并校验固定版本的 OpenAI 官方
Codex，随后将它注入 Session sandbox。产品镜像只安装该
Wheel，不包含 Rust 或 Codex 源码。FastAPI、Uvicorn、`efferva[opensandbox]` 仍由本目录
的 `pyproject.toml` 明确声明。

打开 <http://localhost:8080>。停止服务但保留 PostgreSQL、OpenSandbox 元数据和 Session
工作区：

```bash
docker compose --file examples/basic-local-docker/compose.yaml down
```

连同本地示例数据一起删除：

```bash
docker compose --file examples/basic-local-docker/compose.yaml down --volumes
```

## 接入方代码

产品代码位于 `src/basic_local_docker/main.py`。本地示例使用一个固定的开发身份，因此不需要
先实现登录；生产产品应把自己的 Cookie、JWT 或 SSO 身份转换成 `Principal`。

OpenSandbox 相关参数由 Compose 通过环境变量提供：

```text
EFFERVA_SANDBOX_PROVIDER=opensandbox
EFFERVA_OPENSANDBOX_SERVER_URL=http://opensandbox-server:8090
EFFERVA_OPENSANDBOX_API_KEY=local-dev-key
EFFERVA_OPENSANDBOX_USE_SERVER_PROXY=true
```

标准 HTTP/HTTPS 模型端点默认启用 OpenSandbox Credential Proxy，真实 API Key 不进入
sandbox 环境变量。像本地 `cliproxyapi` 这类非 80/443 端口会自动退回开发凭证环境变量；
Base URL 和 Key 直接通过启动 `make docker-up` 的环境传入即可。
