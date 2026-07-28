#!/usr/bin/env bash
set -euo pipefail

cluster_name="${AGENTFRAME_KIND_CLUSTER:-agentframe}"
context="kind-${cluster_name}"
namespace="agentframe"

kubectl --context "${context}" --namespace "${namespace}" rollout status \
  deployment/agentframe --timeout=180s
app_pods="$(kubectl --context "${context}" --namespace "${namespace}" get pods \
  --selector app=agentframe \
  --output json \
  | jq --raw-output \
    '.items[]
      | select(.metadata.deletionTimestamp == null)
      | select(.status.phase == "Running")
      | select(any(.status.containerStatuses[]?; .ready))
      | .metadata.name' \
  | tr '\n' ' ')"
test "$(wc -w <<<"${app_pods}" | tr -d ' ')" -eq 2
read -r app_pod_one app_pod_two <<<"${app_pods}"

alice_header="x-agentframe-demo-user: alice"
bob_header="x-agentframe-demo-user: bob"
admin_header="x-agentframe-demo-user: admin"
other_admin_header="x-agentframe-demo-user: other-admin"
session_json="$(kubectl --context "${context}" --namespace "${namespace}" exec "${app_pod_one}" -- \
  curl --fail --silent \
    --header "${alice_header}" \
    --header "content-type: application/json" \
    --data '{"name":"kind-smoke"}' \
    http://localhost:8080/api/sessions)"
session_id="$(jq --raw-output .id <<<"${session_json}")"

kubectl --context "${context}" --namespace "${namespace}" exec "${app_pod_two}" -- \
  curl --fail --silent \
    --header "${alice_header}" \
    http://localhost:8080/api/sessions \
  | jq --exit-status --arg id "${session_id}" 'any(.id == $id)' >/dev/null

test "$(
  kubectl --context "${context}" --namespace "${namespace}" exec "${app_pod_two}" -- \
    curl --silent \
      --output /dev/null \
      --write-out "%{http_code}" \
      --header "${bob_header}" \
      "http://localhost:8080/api/sessions/${session_id}"
)" = "404"
kubectl --context "${context}" --namespace "${namespace}" exec "${app_pod_two}" -- \
  curl --fail --silent \
    --header "${admin_header}" \
    "http://localhost:8080/api/sessions?scope=tenant" \
  | jq --exit-status --arg id "${session_id}" 'any(.id == $id)' >/dev/null
test "$(
  kubectl --context "${context}" --namespace "${namespace}" exec "${app_pod_two}" -- \
    curl --silent \
      --output /dev/null \
      --write-out "%{http_code}" \
      --header "${admin_header}" \
      --header "content-type: application/json" \
      --data '{"title":"must remain read-only"}' \
      "http://localhost:8080/api/sessions/${session_id}/threads"
)" = "404"
kubectl --context "${context}" --namespace "${namespace}" exec "${app_pod_two}" -- \
  curl --fail --silent \
    --header "${other_admin_header}" \
    "http://localhost:8080/api/sessions?scope=tenant" \
  | jq --exit-status --arg id "${session_id}" 'all(.id != $id)' >/dev/null

thread_id="$(kubectl --context "${context}" --namespace "${namespace}" exec "${app_pod_two}" -- \
  curl --fail --silent \
    --header "${alice_header}" \
    --header "content-type: application/json" \
    --data '{"title":"kind backend"}' \
    "http://localhost:8080/api/sessions/${session_id}/threads" | jq --raw-output .id)"
run_id="$(kubectl --context "${context}" --namespace "${namespace}" exec "${app_pod_one}" -- \
  curl --fail --silent \
    --header "${alice_header}" \
    --header "content-type: application/json" \
    --data '{"prompt":"Reply with kind-ok."}' \
    "http://localhost:8080/api/threads/${thread_id}/runs" | jq --raw-output .id)"

sandbox_name="af-sandbox-${session_id//-/}"
sandbox_name="${sandbox_name:0:31}"
kubectl --context "${context}" --namespace "${namespace}" wait \
  --for=create \
  --for=condition=Ready \
  "pod/${sandbox_name}" \
  --timeout=180s
kubectl --context "${context}" --namespace "${namespace}" get \
  "service/${sandbox_name}" "persistentvolumeclaim/${sandbox_name}" >/dev/null

status="$(kubectl --context "${context}" --namespace "${namespace}" exec "${app_pod_two}" -- \
  curl --fail --silent \
    --header "${alice_header}" \
    "http://localhost:8080/api/runs/${run_id}" | jq --raw-output .status)"
case "${status}" in
  queued | running | completed | failed) ;;
  *)
    echo "Unexpected Run status: ${status}" >&2
    exit 1
    ;;
esac
kubectl --context "${context}" --namespace "${namespace}" exec "${app_pod_two}" -- \
  curl --fail --silent \
    --header "${alice_header}" \
    "http://localhost:8080/api/runs/${run_id}/events?after=0" \
  | jq --exit-status 'any(.event.type == "RUN_STARTED")' >/dev/null
sse_replay="$(
  kubectl --context "${context}" --namespace "${namespace}" exec "${app_pod_two}" -- \
    curl --silent \
      --max-time 3 \
      --header "${alice_header}" \
      --header "Last-Event-ID: 0" \
      "http://localhost:8080/api/runs/${run_id}/events/stream" \
    || true
)"
grep -q '"type":"RUN_STARTED"' <<<"${sse_replay}"

echo "Kind smoke passed: two instances enforce tenant isolation and replay durable Run events."
