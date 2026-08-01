CREATE TABLE IF NOT EXISTS industry_research_reports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT now(),
    session_id uuid,
    thread_id text,
    model text,
    reasoning_effort text,
    markdown text NOT NULL
);

ALTER TABLE industry_research_reports
    ADD COLUMN IF NOT EXISTS session_id uuid;

ALTER TABLE industry_research_reports
    ADD COLUMN IF NOT EXISTS thread_id text;

ALTER TABLE industry_research_reports
    ADD COLUMN IF NOT EXISTS model text;

ALTER TABLE industry_research_reports
    ADD COLUMN IF NOT EXISTS reasoning_effort text;

CREATE TABLE IF NOT EXISTS industry_research_schedules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    industry text NOT NULL,
    cron_expression text NOT NULL,
    timezone text NOT NULL DEFAULT 'Asia/Shanghai',
    model text NOT NULL,
    reasoning_effort text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    next_run_at timestamptz,
    last_run_at timestamptz
);

CREATE TABLE IF NOT EXISTS industry_research_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT now(),
    scheduled_for timestamptz,
    started_at timestamptz,
    finished_at timestamptz,
    schedule_id uuid REFERENCES industry_research_schedules(id),
    trigger text NOT NULL CHECK (trigger IN ('manual', 'scheduled')),
    industry text NOT NULL,
    status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    stage text NOT NULL DEFAULT 'queued',
    session_id uuid,
    thread_id text,
    model text NOT NULL,
    reasoning_effort text NOT NULL,
    report_id uuid UNIQUE REFERENCES industry_research_reports(id),
    error text,
    UNIQUE (schedule_id, scheduled_for)
);

CREATE INDEX IF NOT EXISTS industry_research_schedules_due_idx
    ON industry_research_schedules (next_run_at)
    WHERE enabled;

CREATE INDEX IF NOT EXISTS industry_research_runs_created_idx
    ON industry_research_runs (created_at DESC);

CREATE INDEX IF NOT EXISTS industry_research_runs_active_idx
    ON industry_research_runs (status, created_at DESC)
    WHERE status IN ('queued', 'running');
