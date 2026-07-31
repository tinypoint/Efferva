CREATE TABLE app_sessions (
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

CREATE INDEX app_sessions_owner_updated_idx
    ON app_sessions(
        tenant_id,
        owner_issuer,
        owner_subject,
        updated_at DESC,
        created_at DESC
    );

CREATE INDEX app_sessions_tenant_updated_idx
    ON app_sessions(tenant_id, updated_at DESC, created_at DESC);

CREATE INDEX app_sessions_last_active_idx
    ON app_sessions(last_active_at);
