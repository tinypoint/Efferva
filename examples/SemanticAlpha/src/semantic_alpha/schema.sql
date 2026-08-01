CREATE TABLE IF NOT EXISTS industry_research_reports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT now(),
    session_id uuid,
    thread_id text,
    markdown text NOT NULL
);

ALTER TABLE industry_research_reports
    ADD COLUMN IF NOT EXISTS session_id uuid;

ALTER TABLE industry_research_reports
    ADD COLUMN IF NOT EXISTS thread_id text;
