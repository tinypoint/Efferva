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
- 本地用开发 Cookie 区分用户；GCP 用经过签名验证的 IAP JWT 映射
  `Efferva.Principal`。
- Efferva 为每个 Session 创建 OpenSandbox，并在启动 Codex 时注入模型配置和
  Vibe-Trading 工具配置。

当前还是一个纵向切片：Session 创建/列表/消息/事件已经接入；取消、重命名、删除以及
Vibe-Trading 的全局 Goal、账户和产物状态还需要继续改造成按 Principal 隔离。未完成的
接口会明确返回 501，不会假装已支持多租户。

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
