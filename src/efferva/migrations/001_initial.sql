CREATE TABLE app_sessions (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    workspace_ref text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app_threads (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    codex_thread_id uuid UNIQUE,
    title text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX app_threads_session_created_idx
    ON app_threads(session_id, created_at DESC);

CREATE TABLE runs (
    id uuid PRIMARY KEY,
    agui_run_id text NOT NULL UNIQUE,
    thread_id uuid NOT NULL REFERENCES app_threads(id) ON DELETE CASCADE,
    status text NOT NULL,
    input_json jsonb NOT NULL,
    codex_turn_id text,
    owner_id text,
    lease_epoch bigint,
    error text,
    last_seq bigint NOT NULL DEFAULT 0,
    terminal_seq bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX runs_status_created_idx ON runs(status, created_at);
CREATE INDEX runs_thread_created_idx ON runs(thread_id, created_at DESC);

CREATE TABLE run_events (
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq bigint NOT NULL,
    event_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE messages (
    id uuid PRIMARY KEY,
    external_id text NOT NULL UNIQUE,
    thread_id uuid NOT NULL REFERENCES app_threads(id) ON DELETE CASCADE,
    run_id uuid REFERENCES runs(id) ON DELETE SET NULL,
    role text NOT NULL,
    content text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'completed',
    first_seq bigint,
    last_seq bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX messages_thread_created_idx ON messages(thread_id, created_at);

CREATE TABLE session_leases (
    session_id uuid PRIMARY KEY REFERENCES app_sessions(id) ON DELETE CASCADE,
    owner_id text NOT NULL,
    fencing_epoch bigint NOT NULL,
    expires_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sandbox_bindings (
    session_id uuid PRIMARY KEY REFERENCES app_sessions(id) ON DELETE CASCADE,
    backend text NOT NULL,
    sandbox_id text NOT NULL,
    endpoint text NOT NULL,
    workspace_ref text NOT NULL,
    status text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
