ALTER TABLE app_sessions
    ADD COLUMN codex_version text NOT NULL DEFAULT 'legacy',
    ADD COLUMN codex_runtime_sha256 text NOT NULL DEFAULT 'legacy';

CREATE INDEX sandbox_leases_idle_reap_idx
    ON sandbox_leases(status, updated_at)
    WHERE status = 'idle';
