# Vibe-Trading on Efferva

这是一个真实产品接入示例，不是重新写的简化聊天页。浏览器里仍然运行
[HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 的 React 产品；
Efferva 接管多用户身份、Session、Thread、Run、持久事件和每个 Session 的沙箱。

所有 Efferva 接入改动都在本目录：

```text
vibe-trading-gcp/
├── overlay/                 # 接入方新增的真实 Python 代码
├── integration.patch        # 对 Vibe-Trading 三个入口文件的最小修改
├── Dockerfile               # 拉取固定上游版本并合入上述代码
├── compose.yaml             # 本地 PostgreSQL + OpenSandbox + 完整产品
├── opensandbox.toml         # OpenSandbox Docker provider
└── gcp/                     # GKE/IAP/Cloud SQL 部署清单
```

旁边是否存在 `Vibe-Trading` 工作区不影响构建。Dockerfile 固定拉取上游 commit
`0e004d7a98097a01751c5dd6adafa1a3afa3817d`，然后只应用本目录可审查的接入代码。

## 接入边界

- Vibe-Trading 原有前端、研究页、回测页、报告页和设置页保持不变。
- 原 `/sessions`、消息和 SSE 契约由
  `overlay/agent/src/efferva_product/compat_routes.py` 适配到 Efferva。
- `calculate_position_size` 作为普通 App 端 Python Tool 注册；`run_workflow`
  作为 Workflow 统一入口注册。二者都通过 Codex `dynamicTools` 和
  `item/tool/call` 运行，不启动 MCP Server。
- `run_workflow(workflow="vibe_research", ...)` 调用 Vibe-Trading 原有
  `SwarmTool`，保留其 preset 选择、DAG 和 worker 实现。Swarm 运行目录由可信的
  tenant/Session 上下文划分，模型不能伪造。
- Vibe-Trading 自带 Skills 从沙箱镜像的 `/app/agent/src/skills` 扫描；
  用户自己的 Skills 可写到该 Session 的 `/workspace/.agents/skills`。两者通过
  `selectedCapabilityRoots` 交给 Codex，均可在创建 Thread 时开关。
- 本地用开发 Cookie 区分用户；GCP 用经过签名验证的 IAP JWT 映射
  `Efferva.Principal`。
- Efferva 为每个 Session 创建 OpenSandbox，并在启动 Codex 时注入模型配置、
  简短 developer instructions、Skills 和 Dynamic Tools。模型及推理强度还可按
  Thread 或 Run 覆盖。
- 研究 Goal 使用 Codex 原生 Goal；取消请求先持久化到 PostgreSQL，再由实际持有
  Run lease 的 worker 中断 Codex turn。
- Codex 可调用内置 `publish_artifact` 发布 Session workspace 中的报告、图表或数据；
  产物内容和 SHA-256 持久化到 PostgreSQL，可从任意 App 副本下载。

当前仍未把 Vibe-Trading 的证据账本、账户、产物卡片 UI、重命名和删除迁入 Efferva。
这些产品状态不能继续使用原项目的全局文件目录，后续必须按 Principal/Session 隔离；
未完成的接口会明确返回 501。Codex 原生 Memory 默认关闭，因为共享 App Runtime 的
`CODEX_HOME` 不是租户隔离边界；只有部署成隔离 Runtime 后才能显式开启。

## 本地启动

需要一个由当前 Efferva `main` 构建的 Linux Wheel。先在工作区根目录构建：

```bash
cd Efferva
./scripts/build-docker-wheel.sh
python3 -m http.server 8765 --directory dist/docker
```

复制并修改环境变量：

```bash
cd examples/vibe-trading-gcp
cp .env.example .env
```

`OPENAI_API_KEY` 填 `cli-proxy-api` 的 key；默认 Base URL 已指向宿主机映射的
`http://host.docker.internal:8317/v1`。Apple Silicon 使用示例中的
`linux_aarch64` Wheel，x86_64 主机改为 `linux_x86_64`。

先构建应用镜像和每 Session 使用的沙箱镜像，再启动：

```bash
docker compose --profile images build
docker compose up
```

打开：

- <http://localhost:8899/dev/login/alice>
- <http://localhost:8899/dev/login/bob>

两个地址会进入同一个 Vibe-Trading 产品，但 Session 数据按 Efferva Principal 隔离。
可以发送下面的消息验证原生 Tool 调用：

```text
账户资金 100000，单笔风险 1%，计划 100 买入、95 止损。
请必须调用 calculate_position_size 工具计算最大仓位。
```

验证 Workflow：

```text
请调用 run_workflow，workflow 使用 vibe_research，
研究英伟达未来 12 个月的基本面、估值和主要风险。
```

停止服务但保留数据：

```bash
docker compose down
```

删除本示例的 PostgreSQL、OpenSandbox 和产品数据卷：

```bash
docker compose down --volumes
```

## GCP

生产部署清单在 `gcp/`。它使用 GKE、Google Cloud IAP、Cloud SQL for PostgreSQL
和 OpenSandbox Kubernetes runtime；具体变量和部署顺序见 `gcp/README.md`。
