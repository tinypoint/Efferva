# Scheduled Investment Research

Scheduled Investment Research 是一个基于 Efferva 的定时投资研究示例。服务端沿用本地
Docker example 的 FastAPI、Efferva、PostgreSQL、Redis 与 OpenSandbox 运行链路，Web UI
以投资月历作为研究报告入口。

每个 Agent Session 会自动获得
[AI Berkshire](https://github.com/xbtlin/ai-berkshire) 的 21 个 Codex skills，
以及这些 skills 直接依赖的财务验算工具。资源固定在上游提交
`0310788cdabb0d724ac9f67e3dbd3e9e4a13d06a`，不依赖宿主机的 Codex 配置。

核心关系只有三层：`ReportTask → ReportRun → Report`。`report_tasks` 保存定时频率、
完整 Prompt、输出文件名、模型、推理深度、报告类型、主题和所属用户；每次到点后，
调度器把这些字段快照到 `report_runs`，再创建 Efferva Session 与 Thread。成功生成的
Markdown 作为不可变快照写入 `reports`，运行记录同时保留耗时和完整线程轨迹。

系统没有手动报告入口。任务通过 `POST /api/report-tasks` 创建或通过
`PATCH /api/report-tasks/{task_id}` 修改；`GET /api/report-runs` 返回当前用户的所有
定时运行，`GET /api/report-runs/{run_id}` 同时返回运行元数据和生成后的 Markdown。

Web 首页是同一批 `ReportRun` 的两种视图：FullCalendar 月历和 TanStack Table，
由筛选器右侧的图标组切换。月历始终按 `scheduled_for` 归属日期，不使用报告入库时间；
筛选维度是“报告类型 + 主题”。两种视图都进入同一个左右分栏详情页：左侧渲染
Markdown，右侧展示原始 Session、Thread、模型、推理深度、请求、推理和工具轨迹。

## 启动

要求：

- Docker Desktop 或 Docker Engine；
- 可用的 `OPENAI_API_KEY`；
- 已在仓库根目录构建 Efferva Wheel。

在仓库根目录执行：

```bash
uv build --wheel --out-dir dist
OPENAI_API_KEY=... docker compose \
  --file examples/scheduled-investment-research/compose.yaml \
  up --build
```

打开 <http://localhost:8080>。

开发 Web UI 时增加 `compose.dev.yaml`：

```bash
OPENAI_API_KEY=... docker compose \
  --file examples/scheduled-investment-research/compose.yaml \
  --file examples/scheduled-investment-research/compose.dev.yaml \
  up --build
```

开发页面位于 <http://localhost:5173>。
