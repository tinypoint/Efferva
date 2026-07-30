ALTER TABLE app_threads
    ADD COLUMN runtime_config_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN goal_json jsonb;

ALTER TABLE runs
    ADD COLUMN cancel_requested_at timestamptz;
