#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${script_dir}/.." && pwd)"
workspace_dir="$(cd "${project_dir}/.." && pwd)"
cluster_name="${EFFERVA_KIND_CLUSTER:-efferva}"

if [[ "${EFFERVA_SKIP_WHEEL_BUILD:-0}" != "1" ]]; then
  "${project_dir}/scripts/build-docker-wheel.sh"
fi

if ! kind get clusters | grep -qx "${cluster_name}"; then
  kind create cluster --name "${cluster_name}" --config "${project_dir}/deploy/kind/cluster.yaml"
fi
kubectl config use-context "kind-${cluster_name}" >/dev/null
kubectl create namespace efferva \
  --dry-run=client \
  --output=yaml | kubectl apply --filename=-

secret_updated=false
if [[ -n "${OPENAI_API_KEY:-}" ]]; then
  secret_args=(
    --from-literal="api-key=${OPENAI_API_KEY}"
  )
  if [[ -n "${EFFERVA_CODEX_OPENAI_BASE_URL:-}" ]]; then
    secret_args+=(--from-literal="base-url=${EFFERVA_CODEX_OPENAI_BASE_URL}")
  fi
  if [[ -n "${EFFERVA_CODEX_MODEL:-}" ]]; then
    secret_args+=(--from-literal="model=${EFFERVA_CODEX_MODEL}")
  fi
  kubectl --namespace efferva create secret generic efferva-openai \
    "${secret_args[@]}" \
    --dry-run=client \
    --output=yaml | kubectl apply --filename=-
  secret_updated=true
fi

docker build \
  --file "${project_dir}/docker/Dockerfile" \
  --target app \
  --tag efferva-app:local \
  "${workspace_dir}"
docker build \
  --file "${project_dir}/docker/Dockerfile" \
  --target sandbox \
  --tag efferva-sandbox:local \
  "${workspace_dir}"

kind load docker-image --name "${cluster_name}" efferva-app:local efferva-sandbox:local
kubectl apply --filename "${project_dir}/deploy/kind/platform.yaml"

if [[ "${secret_updated}" == "true" ]]; then
  kubectl --namespace efferva rollout restart deployment/efferva
fi

kubectl --namespace efferva rollout status statefulset/postgres --timeout=180s
kubectl --namespace efferva rollout status deployment/efferva --timeout=180s
kubectl --namespace efferva get pods

echo "Kind is ready. Run: make kind-port-forward"
