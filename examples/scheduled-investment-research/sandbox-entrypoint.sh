#!/bin/sh
set -eu

session_path=/home/sandbox
template_path=/opt/efferva/session-template

# /home/sandbox is the persistent Session Volume. Refresh only product-owned
# resources from the image; reports, Codex threads, and user files stay intact.
mkdir -p "${session_path}" "${session_path}/workspace/reports"
cp -a "${template_path}/." "${session_path}/"
chown -R 1000:1000 \
    "${session_path}/.codex/skills" \
    "${session_path}/workspace/AGENTS.md" \
    "${session_path}/workspace/AI_BERKSHIRE_LICENSE" \
    "${session_path}/workspace/tools" \
    "${session_path}/workspace/reports"

exec "$@"
