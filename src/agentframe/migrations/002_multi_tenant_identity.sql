ALTER TABLE app_sessions
    ADD COLUMN tenant_id text,
    ADD COLUMN owner_issuer text,
    ADD COLUMN owner_subject text;

UPDATE app_sessions
SET tenant_id = '__agentframe_legacy__',
    owner_issuer = 'agentframe:legacy',
    owner_subject = 'unowned';

ALTER TABLE app_sessions
    ALTER COLUMN tenant_id SET NOT NULL,
    ALTER COLUMN owner_issuer SET NOT NULL,
    ALTER COLUMN owner_subject SET NOT NULL;

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

ALTER TABLE runs
    DROP CONSTRAINT runs_agui_run_id_key;

ALTER TABLE runs
    ADD CONSTRAINT runs_thread_agui_run_id_key UNIQUE (thread_id, agui_run_id);

ALTER TABLE messages
    DROP CONSTRAINT messages_external_id_key;

ALTER TABLE messages
    ADD CONSTRAINT messages_thread_external_id_key UNIQUE (thread_id, external_id);
