# Semantic Alpha

Semantic Alpha 是一个基于 Efferva 的 AI 投资语义示例。服务端沿用本地 Docker
example 的 FastAPI、Efferva、PostgreSQL、Redis 与 OpenSandbox 运行链路，Web UI
只展示一个投资月历。

日历中的公司、事件和信号是演示数据，不构成投资建议。

## 启动

要求：

- Docker Desktop 或 Docker Engine；
- 可用的 `OPENAI_API_KEY`；
- 已在仓库根目录构建 Efferva Wheel。

在仓库根目录执行：

```bash
uv build --wheel --out-dir dist
OPENAI_API_KEY=... docker compose \
  --file examples/SemanticAlpha/compose.yaml \
  up --build
```

打开 <http://localhost:8080>。

开发 Web UI 时增加 `compose.dev.yaml`：

```bash
OPENAI_API_KEY=... docker compose \
  --file examples/SemanticAlpha/compose.yaml \
  --file examples/SemanticAlpha/compose.dev.yaml \
  up --build
```

开发页面位于 <http://localhost:5173>。
