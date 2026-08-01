# Semantic Alpha

Semantic Alpha 是一个基于 Efferva 的 AI 投资语义示例。服务端沿用本地 Docker
example 的 FastAPI、Efferva、PostgreSQL、Redis 与 OpenSandbox 运行链路，Web UI
只展示一个投资月历。

每个 Agent Session 会自动获得
[AI Berkshire](https://github.com/xbtlin/ai-berkshire) 的 21 个 Codex skills，
以及这些 skills 直接依赖的财务验算工具。资源固定在上游提交
`0310788cdabb0d724ac9f67e3dbd3e9e4a13d06a`，不依赖宿主机的 Codex 配置。

日历中的公司、事件和信号是演示数据，不构成投资建议。

产业调查以不可变 Markdown 快照保存在 `industry_research_reports` 表中；修改内容时
新增一行，保留原始调查记录。

调用 `POST /api/industry-reports` 并传入 `{"industry": "AI基建"}`，服务会依次创建
Efferva Session 与 Thread、调用 `$industry-research` 生成 Markdown，并在生成完成后
将全文写入 `industry_research_reports`。

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
