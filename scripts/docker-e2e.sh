#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${script_dir}/.." && pwd)"
http_port="${AGENTFRAME_E2E_PORT:-18080}"
base_url="http://localhost:${http_port}"
export AGENTFRAME_HTTP_PORT="${http_port}"
compose_args=(
  --project-name agentframe-e2e
  --file "${project_dir}/compose.yaml"
  --file "${project_dir}/tests/e2e/compose.mock.yaml"
)
sandbox_name=""
workspace_volume=""
mock_pid=""

cleanup() {
  if [[ -n "${mock_pid}" ]]; then
    kill "${mock_pid}" >/dev/null 2>&1 || true
  fi
  docker compose "${compose_args[@]}" down --volumes >/dev/null 2>&1 || true
  if [[ -n "${sandbox_name}" ]]; then
    docker rm --force "${sandbox_name}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${workspace_volume}" ]]; then
    docker volume rm "${workspace_volume}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

docker network inspect agentframe >/dev/null 2>&1 || docker network create agentframe >/dev/null
python3 "${project_dir}/tests/e2e/mock_responses.py" &
mock_pid="$!"

docker compose "${compose_args[@]}" build app sandbox-image
docker compose "${compose_args[@]}" up --detach --no-build
for _ in $(seq 1 120); do
  if curl --fail --silent "${base_url}/healthz" >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent "${base_url}/healthz" >/dev/null

alice_header="x-agentframe-demo-user: alice"
bob_header="x-agentframe-demo-user: bob"
admin_header="x-agentframe-demo-user: admin"
other_admin_header="x-agentframe-demo-user: other-admin"
session_json="$(curl --fail --silent \
  --header "${alice_header}" \
  --header "content-type: application/json" \
  --data '{"name":"docker-e2e"}' \
  "${base_url}/api/sessions")"
session_id="$(jq --raw-output .id <<<"${session_json}")"
test "$(jq --raw-output .owner_subject <<<"${session_json}")" = "alice"
sandbox_name="af-sandbox-${session_id//-/}"
workspace_volume="af-workspace-${session_id//-/}"

test "$(
  curl --silent \
    --output /dev/null \
    --write-out "%{http_code}" \
    --header "${bob_header}" \
    "${base_url}/api/sessions/${session_id}"
)" = "404"
curl --fail --silent \
  --header "${admin_header}" \
  "${base_url}/api/sessions?scope=tenant" \
  | jq --exit-status --arg id "${session_id}" 'any(.id == $id)' >/dev/null
test "$(
  curl --silent \
    --output /dev/null \
    --write-out "%{http_code}" \
    --header "${admin_header}" \
    --header "content-type: application/json" \
    --data '{"title":"must remain read-only"}' \
    "${base_url}/api/sessions/${session_id}/threads"
)" = "404"
curl --fail --silent \
  --header "${other_admin_header}" \
  "${base_url}/api/sessions?scope=tenant" \
  | jq --exit-status --arg id "${session_id}" 'all(.id != $id)' >/dev/null

thread_id="$(curl --fail --silent \
  --header "${alice_header}" \
  --header "content-type: application/json" \
  --data '{"title":"sandbox proof"}' \
  "${base_url}/api/sessions/${session_id}/threads" | jq --raw-output .id)"
test "$(
  curl --silent \
    --output /dev/null \
    --write-out "%{http_code}" \
    --header "${admin_header}" \
    --header "content-type: application/json" \
    --data '{"prompt":"read-only admin bypass"}' \
    "${base_url}/api/threads/${thread_id}/runs"
)" = "404"
test "$(
  curl --silent \
    --output /dev/null \
    --write-out "%{http_code}" \
    --header "${bob_header}" \
    "${base_url}/api/threads/${thread_id}"
)" = "404"
test "$(
  curl --silent \
    --output /dev/null \
    --write-out "%{http_code}" \
    --header "${other_admin_header}" \
    "${base_url}/api/threads/${thread_id}"
)" = "404"
agui_bypass_payload="$(
  jq --compact-output --null-input \
    --arg thread_id "${thread_id}" \
    '{
      threadId: $thread_id,
      runId: "cross-user-bypass",
      messages: [{id: "bypass-message", role: "user", content: "bypass"}]
    }'
)"
test "$(
  curl --silent \
    --output /dev/null \
    --write-out "%{http_code}" \
    --header "${bob_header}" \
    --header "content-type: application/json" \
    --data "${agui_bypass_payload}" \
    "${base_url}/api/ag-ui"
)" = "404"
run_id="$(curl --fail --silent \
  --header "${alice_header}" \
  --header "content-type: application/json" \
  --data '{"prompt":"Create the sandbox proof file."}' \
  "${base_url}/api/threads/${thread_id}/runs" | jq --raw-output .id)"
test "$(
  curl --silent \
    --output /dev/null \
    --write-out "%{http_code}" \
    --header "${bob_header}" \
    "${base_url}/api/runs/${run_id}/events"
)" = "404"
test "$(
  curl --silent \
    --output /dev/null \
    --write-out "%{http_code}" \
    --header "${bob_header}" \
    "${base_url}/api/runs/${run_id}/events/stream"
)" = "404"

for _ in $(seq 1 120); do
  status="$(
    curl --fail --silent \
      --header "${alice_header}" \
      "${base_url}/api/runs/${run_id}" \
      | jq --raw-output .status
  )"
  if [[ "${status}" == "completed" || "${status}" == "failed" ]]; then
    break
  fi
  sleep 1
done

if [[ "${status}" != "completed" ]]; then
  echo "Run did not complete:" >&2
  curl --fail --silent \
    --header "${alice_header}" \
    "${base_url}/api/runs/${run_id}" | jq . >&2
  curl --fail --silent \
    --header "${alice_header}" \
    "${base_url}/api/runs/${run_id}/events?after=0" | jq . >&2
  docker compose "${compose_args[@]}" logs app >&2
  exit 1
fi

if ! proof="$(docker exec "${sandbox_name}" cat /workspace/proof.txt 2>/dev/null)" \
  || [[ "${proof}" != "sandbox-ok" ]]; then
  echo "Sandbox proof was not created; durable Run events follow:" >&2
  curl --fail --silent \
    --header "${alice_header}" \
    "${base_url}/api/runs/${run_id}/events?after=0" | jq . >&2
  docker logs "${sandbox_name}" >&2
  docker compose "${compose_args[@]}" logs app >&2
  exit 1
fi
curl --fail --silent \
  --header "${alice_header}" \
  "${base_url}/api/threads/${thread_id}" \
  | jq --exit-status '.messages | any(.role == "assistant" and .content == "sandbox-ok")' \
  >/dev/null
curl --fail --silent \
  --header "${alice_header}" \
  "${base_url}/api/runs/${run_id}/events?after=0" \
  | jq --exit-status 'any(.event.type == "RUN_FINISHED")' \
  >/dev/null
replayed="$(
  curl --fail --silent \
    --header "${alice_header}" \
    --header "Last-Event-ID: 1" \
    "${base_url}/api/runs/${run_id}/events/stream"
)"
grep -q '"type":"RUN_FINISHED"' <<<"${replayed}"
if grep -q '"type":"RUN_STARTED"' <<<"${replayed}"; then
  echo "SSE replay ignored Last-Event-ID" >&2
  exit 1
fi

echo "Docker E2E passed: tenant isolation, durable Run, sandbox execution, and SSE replay."
