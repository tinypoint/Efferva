# Multi-session Codex

这是 Efferva 的多 Session Codex 示例：两个产品 App 实例共享 PostgreSQL，而每个 Session
拥有独立的 Sandbox、Codex app-server、workspace 和 `CODEX_HOME`。

它既可以通过 Docker Compose 在本机快速开发，也可以部署到 Kind，实际验证多个 App Pod
并发访问同一组 Session。OpenSandbox 是当前的 Sandbox Provider；Compose 使用 Docker
runtime，Kind 使用 Kubernetes runtime。

这个目录拥有自己的 `pyproject.toml`，直接声明产品代码使用的 Efferva、FastAPI 和
Uvicorn，不依赖 Efferva 仓库的开发环境或传递依赖。

## 架构

```text
Browser
  │
  ▼
multi-session-codex app × N
  ├── Efferva App / Run Worker
  ├── PostgreSQL
  └── OpenSandbox Python SDK
          │
          ▼
      OpenSandbox Server
          │ Docker runtime
          ▼
      one Codex app-server + persistent volume per Session
```

OpenSandbox Server 通过挂载的 Docker socket 创建沙箱；Efferva 不直接操作 Docker
daemon。每个 Session 对应一个 OpenSandbox sandbox，并把持久卷挂载到 `/session`：
工作区为 `/session/workspace`，Codex 原生状态为 `/session/codex-home`。

## 启动

要求：

- Docker Desktop 或 Docker Engine；
- 可用的 `OPENAI_API_KEY`；
- 已通过根目录 `uv build --wheel --out-dir dist` 构建 Efferva 纯 Python Wheel。

在仓库根目录执行：

```bash
uv build --wheel --out-dir dist
OPENAI_API_KEY=... docker compose \
  --file examples/multi-session-codex/compose.yaml \
  up --build
```

`uv build --wheel --out-dir dist` 只构建 Efferva Python 包；产品启动时下载并校验固定版本的 OpenAI 官方
Codex，随后将它注入 Session sandbox。产品镜像只安装该
Wheel，不包含 Rust 或 Codex 源码。FastAPI、Uvicorn、`efferva[opensandbox]` 仍由本目录
的 `pyproject.toml` 明确声明。

打开 <http://localhost:8080>。停止服务但保留 PostgreSQL、OpenSandbox 元数据和 Session
工作区：

```bash
docker compose --file examples/multi-session-codex/compose.yaml down
```

连同本地示例数据一起删除：

```bash
docker compose --file examples/multi-session-codex/compose.yaml down --volumes
```

## 接入方代码

产品代码位于 `src/multi_session_codex/main.py`。本地示例使用一个固定的开发身份，因此不需要
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
Base URL 和 Key 直接通过启动 `docker compose` 的环境传入即可。
