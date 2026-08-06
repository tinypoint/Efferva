# Multi-session

一个 FastAPI 产品同时安装两套独立的 Efferva：

```text
/codex  → Codex       → efferva_codex  → Codex Sandbox
/claude → Claude Code → efferva_claude → Claude Sandbox
```

两套内核只共享宿主应用、WebUI、PostgreSQL Server 和 OpenSandbox Server。Session、
Sandbox、Volume、Workspace、Thread 和 Credential Proxy 均互相隔离。

## 启动

先在仓库根目录构建 Efferva Wheel：

```bash
uv build --wheel --out-dir dist
```

使用官方 API：

```bash
OPENAI_API_KEY="your-openai-key" \
ANTHROPIC_API_KEY="your-anthropic-key" \
docker compose --file examples/multi-session/compose.yaml up --build
```

使用本机 `cliproxyapi`：

```bash
OPENAI_API_KEY="your-local-token" \
EFFERVA_CODEX_OPENAI_BASE_URL="http://host.docker.internal:8317/v1" \
EFFERVA_CODEX_CREDENTIAL_PROXY_ENABLED="false" \
ANTHROPIC_API_KEY="your-local-token" \
ANTHROPIC_BASE_URL="http://host.docker.internal:8317" \
ANTHROPIC_MODEL="gpt-5.6-sol" \
EFFERVA_CLAUDE_CREDENTIAL_PROXY_ENABLED="false" \
docker compose --file examples/multi-session/compose.yaml up --build
```

打开 <http://localhost:8080>，左侧可以在 Codex 与 Claude Code 之间切换。首次访问
Claude Session 时会在对应 Sandbox 中安装固定版本的 Claude Agent SDK。

如果运行过旧版 Example，需要让 PostgreSQL 重新执行双 Database 初始化脚本：

```bash
docker compose --file examples/multi-session/compose.yaml down --volumes
```

## 产品代码

核心接入代码位于 `src/multi_session/main.py`：

```python
Efferva(
    config=config.codex.efferva,
    identity=resolve_local_principal,
    sandbox=codex_sandbox,
    engine=config.codex.engine,
).install(app, prefix="/codex")

Efferva(
    config=config.claude.efferva,
    identity=resolve_local_principal,
    sandbox=claude_sandbox,
    engine=config.claude.engine,
).install(app, prefix="/claude")
```

前端只有一套 Session、Thread 和聊天 UI。`CodexAdapter` 处理 WebSocket/JSON-RPC，
`ClaudeAdapter` 处理 HTTP/SSE；两者只投影为 UI 需要的 Message、Thinking 和 Tool Block。

开发模式：

```bash
docker compose \
  --file examples/multi-session/compose.yaml \
  --file examples/multi-session/compose.dev.yaml \
  up --build
```

WebUI 位于 <http://localhost:5173>。
