CREATE TABLE artifacts (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    thread_id uuid NOT NULL REFERENCES app_threads(id) ON DELETE CASCADE,
    session_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    path text NOT NULL,
    name text NOT NULL,
    media_type text NOT NULL,
    size_bytes bigint NOT NULL,
    sha256 text NOT NULL,
    content bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(run_id, path)
);

CREATE INDEX artifacts_run_created_idx ON artifacts(run_id, created_at);
