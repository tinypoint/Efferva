# Efferva engineering constraints

- Keep Codex Runtime outside the sandbox. Only command and filesystem execution cross the
  exec-server boundary.
- PostgreSQL is the control-plane source of truth for Session, Thread, Run, durable events, and
  Codex history. Workspace files belong in per-Session volumes/PVCs.
- HTTP and SSE handlers must remain instance-agnostic. Do not introduce sticky-session
  correctness requirements.
- Preserve one active Run per Thread. Different Threads in the same Session may execute in
  parallel under one fenced Session lease.
- Keep the Codex fork thin. Product behavior belongs in this repository; fork changes should
  expose or repair general extension boundaries.
- Schema changes are immutable numbered migrations and must be safe under concurrent App startup.
- Run `uv run ruff check .`, `uv run ruff format --check .`, `cargo test --workspace`, and the
  PostgreSQL integration tests for relevant changes.
