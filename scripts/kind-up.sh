#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${script_dir}/.." && pwd)"
workspace_dir="$(cd "${project_dir}/.." && pwd)"
cluster_name="${AGENTFRAME_KIND_CLUSTER:-agentframe}"

if ! kind get clusters | grep -qx "${cluster_name}"; then
  kind create cluster --name "${cluster_name}" --config "${project_dir}/deploy/kind/cluster.yaml"
fi
kubectl config use-context "kind-${cluster_name}" >/dev/null
kubectl create namespace agentframe \
  --dry-run=client \
  --output=yaml | kubectl apply --filename=-

secret_updated=false
if [[ -n "${OPENAI_API_KEY:-}" ]]; then
  secret_args=(
    --from-literal="api-key=${OPENAI_API_KEY}"
  )
  if [[ -n "${AGENTFRAME_CODEX_OPENAI_BASE_URL:-}" ]]; then
    secret_args+=(--from-literal="base-url=${AGENTFRAME_CODEX_OPENAI_BASE_URL}")
  fi
  if [[ -n "${AGENTFRAME_CODEX_MODEL:-}" ]]; then
    secret_args+=(--from-literal="model=${AGENTFRAME_CODEX_MODEL}")
  fi
  kubectl --namespace agentframe create secret generic agentframe-openai \
    "${secret_args[@]}" \
    --dry-run=client \
    --output=yaml | kubectl apply --filename=-
  secret_updated=true
fi

docker build \
  --file "${project_dir}/docker/Dockerfile" \
  --target app \
  --tag agentframe-app:local \
  "${workspace_dir}"
docker build \
  --file "${project_dir}/docker/Dockerfile" \
  --target sandbox \
  --tag agentframe-sandbox:local \
  "${workspace_dir}"

kind load docker-image --name "${cluster_name}" agentframe-app:local agentframe-sandbox:local
kubectl apply --filename "${project_dir}/deploy/kind/platform.yaml"

if [[ "${secret_updated}" == "true" ]]; then
  kubectl --namespace agentframe rollout restart deployment/agentframe
fi

kubectl --namespace agentframe rollout status statefulset/postgres --timeout=180s
kubectl --namespace agentframe rollout status deployment/agentframe --timeout=180s
kubectl --namespace agentframe get pods

echo "Kind is ready. Run: make kind-port-forward"
