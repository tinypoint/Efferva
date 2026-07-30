# Kind 双实例

第一版 Kind 部署运行两个完全相同的产品 App Pod。PostgreSQL 保存控制面、Run 队列和
可补流事件；每个 Session 的 Codex app-server、workspace 与 `CODEX_HOME` 位于
OpenSandbox 管理的 sandbox/PVC。

```bash
uv build --wheel --out-dir dist
docker compose --file examples/basic-local-docker/compose.yaml build app

kind create cluster \
  --name efferva \
  --config examples/basic-local-docker/kind/cluster.yaml
kind load docker-image efferva-basic-local-docker:local --name efferva

kubectl create namespace opensandbox-system
kubectl create namespace efferva
kubectl create namespace efferva-sandboxes
kubectl create secret generic opensandbox-api-key \
  --namespace opensandbox-system \
  --from-literal=api-key=local-dev-key
kubectl create secret generic efferva-secrets \
  --namespace efferva \
  --from-literal=openai-api-key="$OPENAI_API_KEY"

helm upgrade --install opensandbox \
  https://github.com/alibaba/OpenSandbox/releases/download/helm/opensandbox/0.2.0/opensandbox-0.2.0.tgz \
  --namespace opensandbox-system \
  --values examples/basic-local-docker/kind/opensandbox-values.yaml

kubectl apply --filename examples/basic-local-docker/kind/app.yaml

kubectl --namespace efferva rollout status statefulset/postgres
kubectl --namespace efferva rollout status deployment/efferva
kubectl --namespace efferva port-forward service/efferva 8080:8080
```

打开 <http://localhost:8080>。`kubectl get pods -n efferva` 应显示两个 App Pod；浏览器
SSE 可从任一 Pod 补流，不需要粘性 Session。

OpenSandbox 0.2.0 的 Kind/Helm 安装方式来自其
[Kubernetes 官方说明](https://github.com/alibaba/OpenSandbox/blob/main/kubernetes/README.md)。
