ALTER TABLE app_sessions
    ADD COLUMN last_active_at timestamptz NOT NULL DEFAULT now();

CREATE INDEX app_sessions_last_active_idx
    ON app_sessions(last_active_at);

DROP TABLE IF EXISTS artifacts;
DROP TABLE IF EXISTS messages;
DROP TABLE IF EXISTS run_events;
DROP TABLE IF EXISTS runs;
DROP TABLE IF EXISTS app_threads;
DROP TABLE IF EXISTS session_leases;
DROP TABLE IF EXISTS sandbox_leases;
DROP TABLE IF EXISTS workspace_bindings;

ALTER TABLE app_sessions
    DROP COLUMN workspace_ref;
