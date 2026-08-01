CREATE TABLE IF NOT EXISTS app_sessions (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    owner_issuer text NOT NULL,
    owner_subject text NOT NULL,
    name text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    codex_version text NOT NULL,
    codex_runtime_sha256 text NOT NULL,
    last_active_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS app_sessions_owner_updated_idx
    ON app_sessions(
        tenant_id,
        owner_issuer,
        owner_subject,
        updated_at DESC,
        created_at DESC
    );

CREATE INDEX IF NOT EXISTS app_sessions_tenant_updated_idx
    ON app_sessions(tenant_id, updated_at DESC, created_at DESC);

CREATE INDEX IF NOT EXISTS app_sessions_last_active_idx
    ON app_sessions(last_active_at);

ALTER TABLE app_sessions
    ADD COLUMN IF NOT EXISTS default_model text;

ALTER TABLE app_sessions
    ADD COLUMN IF NOT EXISTS default_reasoning_effort text;

DROP TABLE IF EXISTS thread_execution_settings;

CREATE TABLE IF NOT EXISTS agent_runs (
    id text PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    thread_id text NOT NULL,
    turn_id text,
    status text NOT NULL DEFAULT 'queued',
    worker_id text,
    command jsonb NOT NULL,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_runs_status_created_idx
    ON agent_runs(status, created_at);

CREATE INDEX IF NOT EXISTS agent_runs_thread_updated_idx
    ON agent_runs(session_id, thread_id, updated_at DESC);
