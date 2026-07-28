CREATE SCHEMA IF NOT EXISTS codex_engine;

CREATE TABLE IF NOT EXISTS codex_engine.threads (
    thread_id uuid PRIMARY KEY,
    session_id uuid NOT NULL,
    create_params jsonb NOT NULL,
    metadata_patch jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    recency_at timestamptz NOT NULL DEFAULT now(),
    archived_at timestamptz,
    next_ordinal bigint NOT NULL DEFAULT 0,
    writer_owner uuid,
    writer_epoch bigint NOT NULL DEFAULT 0,
    writer_expires_at timestamptz
);

CREATE INDEX IF NOT EXISTS codex_threads_session_idx
    ON codex_engine.threads(session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS codex_threads_recency_idx
    ON codex_engine.threads(recency_at DESC);

CREATE TABLE IF NOT EXISTS codex_engine.rollout_items (
    thread_id uuid NOT NULL REFERENCES codex_engine.threads(thread_id) ON DELETE CASCADE,
    ordinal bigint NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, ordinal)
);

CREATE TABLE IF NOT EXISTS codex_engine.schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO codex_engine.schema_migrations(version)
VALUES (1)
ON CONFLICT (version) DO NOTHING;
