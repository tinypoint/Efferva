#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${script_dir}/.." && pwd)"
http_port="${EFFERVA_E2E_PORT:-18080}"
base_url="http://localhost:${http_port}/agent"
compose_args=(
  --project-name efferva-opensandbox-e2e
  --file "${project_dir}/tests/e2e/compose.yaml"
)
session_id=""
workspace_volume=""

cleanup() {
  if [[ -n "${session_id}" ]]; then
    docker compose "${compose_args[@]}" exec --no-TTY app \
      python -c '
import asyncio
import os
import sys
from opensandbox import SandboxManager
from opensandbox.config import ConnectionConfig
from opensandbox.models.sandboxes import SandboxFilter

async def cleanup() -> None:
    config = ConnectionConfig(
        domain=os.environ["EFFERVA_OPENSANDBOX_SERVER_URL"],
        api_key=os.environ.get("EFFERVA_OPENSANDBOX_API_KEY"),
        use_server_proxy=True,
        disable_metrics=True,
    )
    manager = await SandboxManager.create(config)
    try:
        result = await manager.list_sandbox_infos(
            SandboxFilter(
                states=["RUNNING", "PAUSED"],
                metadata={"efferva.session": sys.argv[1]},
                page_size=10,
                page=1,
            )
        )
        for sandbox in result.sandbox_infos:
            await manager.kill_sandbox(sandbox.id)
    finally:
        await manager.close()

asyncio.run(cleanup())
' "${session_id}" >/dev/null 2>&1 || true
  fi
  docker compose "${compose_args[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  if [[ -n "${workspace_volume}" ]]; then
    docker volume rm "${workspace_volume}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [[ "${EFFERVA_SKIP_WHEEL_BUILD:-0}" != "1" ]]; then
  "${project_dir}/scripts/build-docker-wheel.sh"
fi

docker compose "${compose_args[@]}" build
docker compose "${compose_args[@]}" up --detach --no-build

for _ in $(seq 1 120); do
  if curl --fail --silent "${base_url}/healthz" >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent "${base_url}/healthz" >/dev/null

session_json="$(
  curl --fail --silent \
    --header "content-type: application/json" \
    --data '{"name":"opensandbox-e2e"}' \
    "${base_url}/api/sessions"
)"
session_id="$(jq --raw-output .id <<<"${session_json}")"
workspace_volume="efferva-workspace-${session_id//-/}"

thread_id="$(
  curl --fail --silent \
    --header "content-type: application/json" \
    --data '{"title":"real product integration"}' \
    "${base_url}/api/sessions/${session_id}/threads" \
  | jq --raw-output .id
)"
run_id="$(
  curl --fail --silent \
    --header "content-type: application/json" \
    --data '{"prompt":"Create the sandbox proof file."}' \
    "${base_url}/api/threads/${thread_id}/runs" \
  | jq --raw-output .id
)"

status="queued"
for _ in $(seq 1 180); do
  status="$(
    curl --fail --silent "${base_url}/api/runs/${run_id}" \
    | jq --raw-output .status
  )"
  if [[ "${status}" == "completed" || "${status}" == "failed" ]]; then
    break
  fi
  sleep 1
done

if [[ "${status}" != "completed" ]]; then
  curl --fail --silent "${base_url}/api/runs/${run_id}/events?after=0" | jq . >&2
  docker compose "${compose_args[@]}" logs app opensandbox-server >&2
  exit 1
fi

curl --fail --silent "${base_url}/api/threads/${thread_id}" \
  | jq --exit-status \
    '.messages | any(.role == "assistant" and .content == "sandbox-ok")' \
  >/dev/null
curl --fail --silent "${base_url}/api/runs/${run_id}/events?after=0" \
  | jq --exit-status 'any(.event.type == "RUN_FINISHED")' \
  >/dev/null

echo "OpenSandbox E2E passed: real FastAPI product, packaged wheel, sandbox execution, and durable events."
