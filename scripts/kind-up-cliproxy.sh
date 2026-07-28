#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${script_dir}/.." && pwd)"
proxy_container="${AGENTFRAME_CLIPROXY_CONTAINER:-cli-proxy-api}"

if ! docker inspect "${proxy_container}" >/dev/null 2>&1; then
  echo "CLIProxyAPI container not found: ${proxy_container}" >&2
  exit 1
fi
if [[ "$(docker inspect --format '{{.State.Running}}' "${proxy_container}")" != "true" ]]; then
  echo "CLIProxyAPI container is not running: ${proxy_container}" >&2
  exit 1
fi

config_path="$(
  docker inspect \
    --format '{{range .Mounts}}{{if eq .Destination "/CLIProxyAPI/config.yaml"}}{{.Source}}{{end}}{{end}}' \
    "${proxy_container}"
)"
if [[ -z "${config_path}" || ! -f "${config_path}" ]]; then
  echo "CLIProxyAPI config.yaml bind mount was not found" >&2
  exit 1
fi

proxy_port="$(
  docker inspect \
    --format '{{(index (index .NetworkSettings.Ports "8317/tcp") 0).HostPort}}' \
    "${proxy_container}"
)"
proxy_api_key="$(
  uv run --project "${project_dir}" python -c \
    'import sys, yaml; keys = yaml.safe_load(open(sys.argv[1]))["api-keys"]; assert keys; print(keys[0], end="")' \
    "${config_path}"
)"

export OPENAI_API_KEY="${proxy_api_key}"
export AGENTFRAME_CODEX_OPENAI_BASE_URL="${AGENTFRAME_CODEX_OPENAI_BASE_URL:-http://host.docker.internal:${proxy_port}/v1}"
export AGENTFRAME_CODEX_MODEL="${AGENTFRAME_CODEX_MODEL:-gpt-5.4}"

exec "${script_dir}/kind-up.sh"
