#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "usage: $0 PATH_TO_WHEEL" >&2
  exit 2
fi

wheel_path="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
wheel_name="$(basename "${wheel_path}")"
if [[ ! -f "${wheel_path}" ]]; then
  echo "wheel not found: ${wheel_path}" >&2
  exit 2
fi

docker run --rm \
  --volume "${wheel_path}:/tmp/${wheel_name}:ro" \
  --env "AGENTFRAME_SMOKE_WHEEL=/tmp/${wheel_name}" \
  python:3.13-slim-bookworm \
  sh -ceu '
    command -v cargo >/dev/null 2>&1 && exit 20
    test ! -e /source/codex-fork
    python -m pip install --quiet "${AGENTFRAME_SMOKE_WHEEL}"
    runtime="$(
      python -c "from agentframe import locate_runtime_binary; print(locate_runtime_binary())"
    )"
    test -x "${runtime}"
    "${runtime}" --help >/dev/null
    python -c "
from agentframe import AgentFrame, runtime_build_info
info = runtime_build_info()
assert info is not None
assert info.codex_revision
assert info.runtime_sha256
print(f\"clean wheel install passed: codex={info.codex_revision[:12]}\")
"
  '
