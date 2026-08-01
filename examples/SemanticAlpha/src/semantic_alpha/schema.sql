CREATE TABLE IF NOT EXISTS industry_research_reports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT now(),
    markdown text NOT NULL
);
