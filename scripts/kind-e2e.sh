#!/usr/bin/env bash
set -euo pipefail

cluster_name="${AGENTFRAME_KIND_CLUSTER:-agentframe}"
context="kind-${cluster_name}"
namespace="agentframe"

kubectl --context "${context}" --namespace "${namespace}" rollout status \
  deployment/agentframe --timeout=180s
app_pods="$(kubectl --context "${context}" --namespace "${namespace}" get pods \
  --selector app=agentframe \
  --field-selector status.phase=Running \
  --output jsonpath='{range .items[*]}{.metadata.name}{" "}{end}')"
read -r pod_one pod_two <<<"${app_pods}"
test -n "${pod_one}"
test -n "${pod_two}"

alice_header="x-agentframe-demo-user: alice"
bob_header="x-agentframe-demo-user: bob"
admin_header="x-agentframe-demo-user: admin"

session_json="$(kubectl --context "${context}" --namespace "${namespace}" \
  exec "${pod_one}" --container app -- \
  curl --fail --silent \
    --header "${alice_header}" \
    --header "content-type: application/json" \
    --data '{"name":"kind-parallel-e2e"}' \
    http://localhost:8080/api/sessions)"
session_id="$(jq --raw-output .id <<<"${session_json}")"

thread_a="$(kubectl --context "${context}" --namespace "${namespace}" \
  exec "${pod_one}" --container app -- \
  curl --fail --silent \
    --header "${alice_header}" \
    --header "content-type: application/json" \
    --data '{"title":"parallel-a"}' \
    "http://localhost:8080/api/sessions/${session_id}/threads" \
  | jq --raw-output .id)"
thread_b="$(kubectl --context "${context}" --namespace "${namespace}" \
  exec "${pod_two}" --container app -- \
  curl --fail --silent \
    --header "${alice_header}" \
    --header "content-type: application/json" \
    --data '{"title":"parallel-b"}' \
    "http://localhost:8080/api/sessions/${session_id}/threads" \
  | jq --raw-output .id)"

test "$(
  kubectl --context "${context}" --namespace "${namespace}" \
    exec "${pod_two}" --container app -- \
    curl --silent \
      --output /dev/null \
      --write-out "%{http_code}" \
      --header "${admin_header}" \
      --header "content-type: application/json" \
      --data '{"prompt":"read-only admin bypass"}' \
      "http://localhost:8080/api/threads/${thread_a}/runs"
)" = "404"
agui_bypass_payload="$(
  jq --compact-output --null-input \
    --arg thread_id "${thread_a}" \
    '{
      threadId: $thread_id,
      runId: "cross-user-bypass",
      messages: [{id: "bypass-message", role: "user", content: "bypass"}]
    }'
)"
test "$(
  kubectl --context "${context}" --namespace "${namespace}" \
    exec "${pod_one}" --container app -- \
    curl --silent \
      --output /dev/null \
      --write-out "%{http_code}" \
      --header "${bob_header}" \
      --header "content-type: application/json" \
      --data "${agui_bypass_payload}" \
      http://localhost:8080/api/ag-ui
)" = "404"

run_a="$(kubectl --context "${context}" --namespace "${namespace}" \
  exec "${pod_one}" --container app -- \
  curl --fail --silent \
    --header "${alice_header}" \
    --header "content-type: application/json" \
    --data '{"prompt":"Create /workspace/thread-a.txt with exact content parallel-a, then reply parallel-a."}' \
    "http://localhost:8080/api/threads/${thread_a}/runs" \
  | jq --raw-output .id)"
run_b="$(kubectl --context "${context}" --namespace "${namespace}" \
  exec "${pod_two}" --container app -- \
  curl --fail --silent \
    --header "${alice_header}" \
    --header "content-type: application/json" \
    --data '{"prompt":"Create /workspace/thread-b.txt with exact content parallel-b, then reply parallel-b."}' \
    "http://localhost:8080/api/threads/${thread_b}/runs" \
  | jq --raw-output .id)"

status_a="queued"
status_b="queued"
for _ in $(seq 1 180); do
  status_a="$(kubectl --context "${context}" --namespace "${namespace}" \
    exec "${pod_two}" --container app -- \
    curl --fail --silent \
      --header "${alice_header}" \
      "http://localhost:8080/api/runs/${run_a}" \
    | jq --raw-output .status)"
  status_b="$(kubectl --context "${context}" --namespace "${namespace}" \
    exec "${pod_one}" --container app -- \
    curl --fail --silent \
      --header "${alice_header}" \
      "http://localhost:8080/api/runs/${run_b}" \
    | jq --raw-output .status)"
  if [[ "${status_a}" =~ ^(completed|failed)$ && "${status_b}" =~ ^(completed|failed)$ ]]; then
    break
  fi
  sleep 1
done

if [[ "${status_a}" != "completed" || "${status_b}" != "completed" ]]; then
  echo "Parallel Runs failed: run-a=${status_a}, run-b=${status_b}" >&2
  kubectl --context "${context}" --namespace "${namespace}" \
    exec "${pod_one}" --container app -- \
    curl --fail --silent \
      --header "${alice_header}" \
      "http://localhost:8080/api/runs/${run_a}/events?after=0" \
    | jq . >&2
  kubectl --context "${context}" --namespace "${namespace}" \
    exec "${pod_two}" --container app -- \
    curl --fail --silent \
      --header "${alice_header}" \
      "http://localhost:8080/api/runs/${run_b}/events?after=0" \
    | jq . >&2
  exit 1
fi

sandbox_name="af-sandbox-${session_id//-/}"
sandbox_name="${sandbox_name:0:31}"
test "$(
  kubectl --context "${context}" --namespace "${namespace}" \
    exec "${sandbox_name}" --container exec-server -- \
    cat /workspace/thread-a.txt
)" = "parallel-a"
test "$(
  kubectl --context "${context}" --namespace "${namespace}" \
    exec "${sandbox_name}" --container exec-server -- \
    cat /workspace/thread-b.txt
)" = "parallel-b"

test "$(
  kubectl --context "${context}" --namespace "${namespace}" \
    exec "${pod_one}" --container app -- \
    curl --silent \
      --output /dev/null \
      --write-out "%{http_code}" \
      --header "${bob_header}" \
      "http://localhost:8080/api/runs/${run_a}"
)" = "404"
test "$(
  kubectl --context "${context}" --namespace "${namespace}" \
    exec "${pod_two}" --container app -- \
    curl --silent \
      --output /dev/null \
      --write-out "%{http_code}" \
      --header "${bob_header}" \
      "http://localhost:8080/api/runs/${run_a}/events/stream"
)" = "404"
kubectl --context "${context}" --namespace "${namespace}" \
  exec "${pod_two}" --container app -- \
  curl --fail --silent \
    --header "${admin_header}" \
    "http://localhost:8080/api/runs/${run_a}" \
  | jq --exit-status '.status == "completed"' >/dev/null

replayed="$(kubectl --context "${context}" --namespace "${namespace}" \
  exec "${pod_two}" --container app -- \
  curl --fail --silent \
    --header "${alice_header}" \
    --header "Last-Event-ID: 1" \
    "http://localhost:8080/api/runs/${run_a}/events/stream")"
grep -q '"type":"RUN_FINISHED"' <<<"${replayed}"
if grep -q '"type":"RUN_STARTED"' <<<"${replayed}"; then
  echo "SSE replay ignored Last-Event-ID" >&2
  exit 1
fi

echo "Kind E2E passed: two Pods, tenant isolation, parallel Threads, shared PVC, and SSE replay."
