CREATE TABLE workspace_bindings (
    workspace_id uuid PRIMARY KEY,
    session_id uuid NOT NULL UNIQUE REFERENCES app_sessions(id) ON DELETE CASCADE,
    provider text NOT NULL,
    external_ref text NOT NULL,
    state_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX workspace_bindings_provider_external_idx
    ON workspace_bindings(provider, external_ref);

CREATE TABLE sandbox_leases (
    sandbox_id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL UNIQUE
        REFERENCES workspace_bindings(workspace_id) ON DELETE CASCADE,
    provider text NOT NULL,
    external_ref text NOT NULL,
    state_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    owner_id text,
    status text NOT NULL,
    fencing_token bigint NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX sandbox_leases_provider_external_idx
    ON sandbox_leases(provider, external_ref);

INSERT INTO workspace_bindings(
    workspace_id,
    session_id,
    provider,
    external_ref,
    state_json,
    status
)
SELECT
    session_id,
    session_id,
    backend,
    workspace_ref,
    '{}'::jsonb,
    status
FROM sandbox_bindings;

INSERT INTO sandbox_leases(
    sandbox_id,
    workspace_id,
    provider,
    external_ref,
    state_json,
    status,
    fencing_token,
    expires_at
)
SELECT
    session_id,
    session_id,
    backend,
    sandbox_id,
    '{}'::jsonb,
    status,
    0,
    now()
FROM sandbox_bindings;

DROP TABLE sandbox_bindings;
