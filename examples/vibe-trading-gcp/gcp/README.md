# GCP deployment

本目录把同一个 Vibe-Trading + Efferva example 部署到 GKE：

- Google Cloud IAP 负责登录，应用验证签名 JWT；
- Cloud SQL for PostgreSQL 保存 Efferva control-plane 数据；
- OpenSandbox Kubernetes runtime 为每个 Session 创建隔离沙箱；
- Artifact Registry 保存 `app` 和 `sandbox` 两个镜像。

## 1. 构建镜像

在 Efferva 仓库根目录执行，使用当前 main 的 Linux Wheel URL：

```bash
docker build \
  --file examples/vibe-trading-gcp/Dockerfile \
  --target app \
  --build-arg EFFERVA_WHEEL_URL=https://example.test/efferva-linux.whl \
  --tag REGION-docker.pkg.dev/PROJECT_ID/vibe-trading/app:TAG .

docker build \
  --file examples/vibe-trading-gcp/Dockerfile \
  --target sandbox \
  --build-arg EFFERVA_WHEEL_URL=https://example.test/efferva-linux.whl \
  --tag REGION-docker.pkg.dev/PROJECT_ID/vibe-trading/sandbox:TAG .
```

推送两个镜像后，把 `app.yaml` 中的 `PROJECT_ID`、`REGION`、`INSTANCE_NAME`、
域名、IAP audience 和镜像 tag 替换为实际值。

## 2. 安装 OpenSandbox

先创建 `opensandbox-system` 和 `vibe-sandboxes` namespace，再使用
`opensandbox-values.yaml` 安装 OpenSandbox Helm chart。生产 API key 应放到
`opensandbox-api-key` Secret，不要提交到仓库。

## 3. 配置应用 Secret

复制 `secrets.example.yaml` 到仓库外，填写：

- Cloud SQL PostgreSQL URL；
- OpenSandbox API key；
- IAP audience；
- 模型代理 Base URL 和 API key。

然后依次应用 Secret 与 `app.yaml`。`app.yaml` 默认两个无状态应用副本，共享
PostgreSQL；Session workspace 由 OpenSandbox 管理，不依赖应用 Pod 本地磁盘。

这份清单是基础部署骨架。正式上线前还应补齐 Workload Identity 权限、NetworkPolicy、
外部 Secret 管理、数据库迁移 Job、备份、监控和按租户的限额策略。
