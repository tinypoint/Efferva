DO $$
BEGIN
    IF to_regclass('public.reports') IS NULL
       AND to_regclass('public.industry_research_reports') IS NOT NULL THEN
        ALTER TABLE industry_research_reports RENAME TO reports;
    END IF;

    IF to_regclass('public.report_tasks') IS NULL THEN
        IF to_regclass('public.report_schedules') IS NOT NULL THEN
            ALTER TABLE report_schedules RENAME TO report_tasks;
        ELSIF to_regclass('public.industry_research_schedules') IS NOT NULL THEN
            ALTER TABLE industry_research_schedules RENAME TO report_tasks;
        END IF;
    END IF;

    IF to_regclass('public.report_runs') IS NULL
       AND to_regclass('public.industry_research_runs') IS NOT NULL THEN
        ALTER TABLE industry_research_runs RENAME TO report_runs;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS reports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT now(),
    owner_user_id text NOT NULL,
    report_type text NOT NULL,
    subject text NOT NULL,
    title text NOT NULL,
    filename text NOT NULL,
    session_id uuid,
    thread_id text,
    model text,
    reasoning_effort text,
    markdown text NOT NULL
);

CREATE TABLE IF NOT EXISTS report_tasks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    owner_user_id text NOT NULL,
    report_type text NOT NULL,
    subject text NOT NULL,
    title text NOT NULL,
    filename text NOT NULL,
    prompt text NOT NULL,
    cron_expression text NOT NULL,
    timezone text NOT NULL DEFAULT 'Asia/Shanghai',
    model text NOT NULL,
    reasoning_effort text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    next_run_at timestamptz,
    last_run_at timestamptz
);

CREATE TABLE IF NOT EXISTS report_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT now(),
    scheduled_for timestamptz NOT NULL,
    started_at timestamptz,
    finished_at timestamptz,
    task_id uuid NOT NULL REFERENCES report_tasks(id),
    owner_user_id text NOT NULL,
    report_type text NOT NULL,
    subject text NOT NULL,
    title text NOT NULL,
    filename text NOT NULL,
    prompt text NOT NULL,
    status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    stage text NOT NULL DEFAULT 'queued',
    session_id uuid,
    thread_id text,
    model text NOT NULL,
    reasoning_effort text NOT NULL,
    report_id uuid UNIQUE REFERENCES reports(id),
    error text,
    UNIQUE (task_id, scheduled_for)
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'report_tasks'
          AND column_name = 'industry'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'report_tasks'
          AND column_name = 'subject'
    ) THEN
        ALTER TABLE report_tasks RENAME COLUMN industry TO subject;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'report_tasks'
          AND column_name = 'skill'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'report_tasks'
          AND column_name = 'report_type'
    ) THEN
        ALTER TABLE report_tasks RENAME COLUMN skill TO report_type;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'report_runs'
          AND column_name = 'schedule_id'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'report_runs'
          AND column_name = 'task_id'
    ) THEN
        ALTER TABLE report_runs RENAME COLUMN schedule_id TO task_id;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'report_runs'
          AND column_name = 'industry'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'report_runs'
          AND column_name = 'subject'
    ) THEN
        ALTER TABLE report_runs RENAME COLUMN industry TO subject;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'report_runs'
          AND column_name = 'skill'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'report_runs'
          AND column_name = 'report_type'
    ) THEN
        ALTER TABLE report_runs RENAME COLUMN skill TO report_type;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'reports'
          AND column_name = 'skill'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'reports'
          AND column_name = 'report_type'
    ) THEN
        ALTER TABLE reports RENAME COLUMN skill TO report_type;
    END IF;
END $$;

ALTER TABLE report_tasks ADD COLUMN IF NOT EXISTS owner_user_id text;
ALTER TABLE report_tasks ADD COLUMN IF NOT EXISTS report_type text;
ALTER TABLE report_tasks ADD COLUMN IF NOT EXISTS title text;
ALTER TABLE report_tasks ADD COLUMN IF NOT EXISTS filename text;
ALTER TABLE report_tasks ADD COLUMN IF NOT EXISTS prompt text;

UPDATE report_tasks
SET
    owner_user_id = COALESCE(owner_user_id, 'developer'),
    report_type = COALESCE(report_type, 'industry-research'),
    title = COALESCE(
        title,
        CASE COALESCE(report_type, 'industry-research')
            WHEN 'industry-research' THEN subject || '产业链投资研究报告'
            WHEN 'investment-research' THEN subject || '投资研究报告'
            ELSE subject || '报告'
        END
    )
WHERE owner_user_id IS NULL OR report_type IS NULL OR title IS NULL;

UPDATE report_tasks
SET filename = COALESCE(filename, title || '.md')
WHERE filename IS NULL;

UPDATE report_tasks
SET prompt = COALESCE(
    prompt,
    '$' || report_type || ' ' || subject || E'\n\n'
    || '这是开发阶段的链路验证。读取并使用对应 Skill，只生成一份极短的中文 Markdown 样例，不执行完整研究。' || E'\n\n'
    || '执行约束：' || E'\n'
    || '1. 不联网、不创建 subagent、不做完整扫描、不做估值、不运行数据审计工具。' || E'\n'
    || '2. 正文控制在 300～500 个中文字符；一级标题必须精确为“# ' || title || '”。' || E'\n'
    || '3. 明确标注“开发样例，未经研究核验”。' || E'\n'
    || '4. 将完整 Markdown 写入绝对路径：/home/sandbox/workspace/' || filename || E'\n'
    || '5. 写入后检查文件存在且非空；最终回复只报告文件路径与字节数。'
)
WHERE prompt IS NULL;

ALTER TABLE report_tasks ALTER COLUMN owner_user_id SET NOT NULL;
ALTER TABLE report_tasks ALTER COLUMN report_type SET NOT NULL;
ALTER TABLE report_tasks ALTER COLUMN title SET NOT NULL;
ALTER TABLE report_tasks ALTER COLUMN filename SET NOT NULL;
ALTER TABLE report_tasks ALTER COLUMN prompt SET NOT NULL;

ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS owner_user_id text;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS report_type text;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS filename text;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS prompt text;

INSERT INTO report_tasks (
    owner_user_id,
    report_type,
    subject,
    title,
    filename,
    prompt,
    cron_expression,
    timezone,
    model,
    reasoning_effort,
    enabled,
    next_run_at
)
SELECT DISTINCT
    'developer',
    COALESCE(run.report_type, 'industry-research'),
    run.subject,
    run.title,
    run.title || '.md',
    '历史报告迁移任务，不用于再次执行。',
    '0 8 * * *',
    'Asia/Shanghai',
    run.model,
    run.reasoning_effort,
    false,
    NULL::timestamptz
FROM report_runs AS run
WHERE NOT EXISTS (
    SELECT 1
    FROM report_tasks AS task
    WHERE task.report_type = COALESCE(run.report_type, 'industry-research')
      AND task.subject = run.subject
);

UPDATE report_runs AS run
SET task_id = (
    SELECT task.id
    FROM report_tasks AS task
    WHERE task.report_type = COALESCE(run.report_type, 'industry-research')
      AND task.subject = run.subject
    ORDER BY task.enabled DESC, task.created_at
    LIMIT 1
)
WHERE run.task_id IS NULL;

UPDATE report_runs AS run
SET
    scheduled_for = COALESCE(run.scheduled_for, run.started_at, run.created_at),
    owner_user_id = task.owner_user_id,
    report_type = task.report_type,
    subject = task.subject,
    title = task.title,
    filename = task.filename,
    prompt = task.prompt
FROM report_tasks AS task
WHERE task.id = run.task_id;

ALTER TABLE report_runs ALTER COLUMN scheduled_for SET NOT NULL;
ALTER TABLE report_runs ALTER COLUMN task_id SET NOT NULL;
ALTER TABLE report_runs ALTER COLUMN owner_user_id SET NOT NULL;
ALTER TABLE report_runs ALTER COLUMN report_type SET NOT NULL;
ALTER TABLE report_runs ALTER COLUMN filename SET NOT NULL;
ALTER TABLE report_runs ALTER COLUMN prompt SET NOT NULL;
ALTER TABLE report_runs DROP COLUMN IF EXISTS trigger;

ALTER TABLE reports ADD COLUMN IF NOT EXISTS owner_user_id text;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS report_type text;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS subject text;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS title text;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS filename text;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS session_id uuid;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS thread_id text;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS model text;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS reasoning_effort text;

UPDATE reports
SET
    owner_user_id = COALESCE(owner_user_id, 'developer'),
    report_type = COALESCE(report_type, 'industry-research'),
    subject = COALESCE(subject, '历史报告'),
    title = COALESCE(title, '历史报告'),
    filename = COALESCE(filename, COALESCE(title, '历史报告') || '.md')
WHERE owner_user_id IS NULL
   OR report_type IS NULL
   OR subject IS NULL
   OR title IS NULL
   OR filename IS NULL;

UPDATE reports AS report
SET
    owner_user_id = run.owner_user_id,
    report_type = run.report_type,
    subject = run.subject,
    title = run.title,
    filename = run.filename
FROM report_runs AS run
WHERE run.report_id = report.id;

ALTER TABLE reports ALTER COLUMN owner_user_id SET NOT NULL;
ALTER TABLE reports ALTER COLUMN report_type SET NOT NULL;
ALTER TABLE reports ALTER COLUMN subject SET NOT NULL;
ALTER TABLE reports ALTER COLUMN title SET NOT NULL;
ALTER TABLE reports ALTER COLUMN filename SET NOT NULL;

DROP INDEX IF EXISTS industry_research_schedules_due_idx;
DROP INDEX IF EXISTS industry_research_runs_created_idx;
DROP INDEX IF EXISTS industry_research_runs_active_idx;
DROP INDEX IF EXISTS report_schedules_due_idx;

CREATE INDEX IF NOT EXISTS report_tasks_due_idx
    ON report_tasks (next_run_at)
    WHERE enabled;

CREATE INDEX IF NOT EXISTS report_runs_scheduled_idx
    ON report_runs (scheduled_for DESC);

CREATE INDEX IF NOT EXISTS report_runs_owner_idx
    ON report_runs (owner_user_id, scheduled_for DESC);

CREATE INDEX IF NOT EXISTS report_runs_active_idx
    ON report_runs (status, scheduled_for DESC)
    WHERE status IN ('queued', 'running');
