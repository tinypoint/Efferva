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
- GitHub Releases 中存在对应版本和架构的 Efferva Linux Wheel。

在仓库根目录执行：

```bash
OPENAI_API_KEY=... docker compose \
  --file examples/basic-local-docker/compose.yaml \
  up --build
```

应用镜像根据 Docker 架构从 GitHub Release 下载 Linux Wheel，不会在
`docker compose up` 时编译 Codex。FastAPI、Uvicorn、`efferva[opensandbox]`
仍由本目录的 `pyproject.toml` 明确声明。

测试尚未正式发布的 Wheel 时，可以覆盖下载地址：

```bash
EFFERVA_WHEEL_URL=https://example.test/efferva-test.whl \
  docker compose --file examples/basic-local-docker/compose.yaml up --build
```

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
