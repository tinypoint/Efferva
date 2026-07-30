#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${script_dir}/.." && pwd)"
workspace_dir="$(cd "${project_dir}/.." && pwd)"
output_dir="${project_dir}/dist/docker"
temporary_dir="$(mktemp -d)"

cleanup() {
  rm -rf "${temporary_dir}"
}
trap cleanup EXIT

codex_revision="$(git -C "${workspace_dir}/codex-fork" rev-parse HEAD)"
efferva_revision="$(git -C "${project_dir}" rev-parse HEAD)"
cache_args=()
if [[ -n "${EFFERVA_DOCKER_CACHE_FROM:-}" ]]; then
  cache_args+=("--cache-from=${EFFERVA_DOCKER_CACHE_FROM}")
fi
if [[ -n "${EFFERVA_DOCKER_CACHE_TO:-}" ]]; then
  cache_args+=("--cache-to=${EFFERVA_DOCKER_CACHE_TO}")
fi

docker buildx build \
  --file "${project_dir}/Wheel.Dockerfile" \
  --target wheel \
  --build-arg "CODEX_REVISION=${codex_revision}" \
  --build-arg "EFFERVA_REVISION=${efferva_revision}" \
  "${cache_args[@]}" \
  --output "type=local,dest=${temporary_dir}" \
  "${workspace_dir}"

mkdir -p "${output_dir}"
find "${output_dir}" -maxdepth 1 -type f -name 'efferva-*.whl' -delete
cp "${temporary_dir}"/efferva-*.whl "${output_dir}/"
