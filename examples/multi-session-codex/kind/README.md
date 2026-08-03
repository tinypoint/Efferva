# Kind 双实例

第一版 Kind 部署运行两个完全相同的产品 App Pod。PostgreSQL 保存控制面、Run 队列和
可补流事件；每个 Session 的 Codex app-server、workspace 与 `CODEX_HOME` 位于
OpenSandbox 管理的 sandbox/PVC。

本地 Kind 不配置 OpenSandbox egress `networkPolicy`，因此清单关闭 Credential Proxy；
Codex app-server 仍从 `efferva-secrets` 获取 `OPENAI_API_KEY`。

```bash
uv build --wheel --out-dir dist
docker compose --file examples/multi-session-codex/compose.yaml build app

kind create cluster \
  --name efferva \
  --config examples/multi-session-codex/kind/cluster.yaml
kind load docker-image efferva-multi-session-codex:local --name efferva

kubectl create namespace opensandbox-system
kubectl create namespace efferva
kubectl create namespace efferva-sandboxes
kubectl create secret generic opensandbox-api-key \
  --namespace opensandbox-system \
  --from-literal=api-key=local-dev-key
kubectl create secret generic efferva-secrets \
  --namespace efferva \
  --from-literal=openai-api-key="$OPENAI_API_KEY"

opensandbox_source_dir="$(mktemp -d)"
git clone --depth 1 --branch helm/opensandbox/0.2.0 \
  https://github.com/alibaba/OpenSandbox.git "$opensandbox_source_dir"
helm dependency update \
  "$opensandbox_source_dir/kubernetes/charts/opensandbox"
helm upgrade --install opensandbox \
  "$opensandbox_source_dir/kubernetes/charts/opensandbox" \
  --namespace opensandbox-system \
  --values examples/multi-session-codex/kind/opensandbox-values.yaml

kubectl apply --filename examples/multi-session-codex/kind/app.yaml

kubectl --namespace opensandbox-system rollout status \
  deployment/opensandbox-controller-manager
kubectl --namespace opensandbox-system rollout status \
  deployment/opensandbox-server
kubectl --namespace efferva rollout status statefulset/postgres
kubectl --namespace efferva rollout status deployment/efferva
kubectl --namespace efferva port-forward service/efferva 8080:8080
```

打开 <http://localhost:8080>。`kubectl get pods -n efferva` 应显示两个 App Pod；浏览器
SSE 可从任一 Pod 补流，不需要粘性 Session。

OpenSandbox 0.2.0 Release 当前只发布源码 Tag，没有可下载的 all-in-one Chart `.tgz`，所以
这里固定该 Tag 并从官方源码构建本地 Chart 依赖。Chart 的使用方式见其
[官方说明](https://github.com/alibaba/OpenSandbox/blob/helm/opensandbox/0.2.0/kubernetes/charts/opensandbox/README.md)。
