#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${script_dir}/.." && pwd)"
workspace_dir="$(cd "${project_dir}/.." && pwd)"
output_dir="${EFFERVA_BUILD_OUTPUT_DIR:-${project_dir}/dist/local}"
profile="${EFFERVA_BUILD_PROFILE:-container}"
runtime_binary="${EFFERVA_BUILD_RUNTIME_BINARY:-}"

if [[ -z "${runtime_binary}" ]]; then
  if command -v cargo >/dev/null 2>&1; then
    cargo_command=(cargo)
  elif command -v rustup >/dev/null 2>&1; then
    active_toolchain="$(rustup show active-toolchain | awk '{print $1}')"
    toolchain_cargo="$(rustup which --toolchain "${active_toolchain}" cargo)"
    export PATH="$(dirname "${toolchain_cargo}"):${PATH}"
    cargo_command=("${toolchain_cargo}")
  else
    echo "cargo is required to build a maintainer wheel" >&2
    exit 1
  fi
  "${cargo_command[@]}" build \
    --manifest-path "${project_dir}/Cargo.toml" \
    --locked \
    --profile "${profile}" \
    --package efferva-codex-runtime
  runtime_binary="${project_dir}/target/${profile}/efferva-codex-runtime"
fi

codex_revision="${EFFERVA_BUILD_CODEX_REVISION:-$(
  git -C "${workspace_dir}/codex" rev-parse HEAD
)}"
efferva_revision="${EFFERVA_BUILD_REVISION:-$(
  git -C "${project_dir}" rev-parse HEAD
)}"

mkdir -p "${output_dir}"
EFFERVA_BUILD_RUNTIME_BINARY="${runtime_binary}" \
EFFERVA_BUILD_CODEX_REVISION="${codex_revision}" \
EFFERVA_BUILD_REVISION="${efferva_revision}" \
  uv build --wheel --out-dir "${output_dir}" "${project_dir}"
