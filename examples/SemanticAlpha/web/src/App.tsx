import { useCallback, useEffect, useMemo, useState } from "react";
import zhCnLocale from "@fullcalendar/react/locales/zh-cn";
import {
  type ColumnDef,
  type SortingState,
  flexRender,
  getCoreRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
} from "@tanstack/react-table";
import {
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  ArrowUpDown,
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  TableProperties,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { EventCalendar } from "@/components/event-calendar";
import { ThreadPanel } from "./ThreadPanel";

type ViewMode = "calendar" | "table";

type ReportTask = {
  id: string;
  report_type: string;
  subject: string;
  title: string;
};

type ReportRun = {
  id: string;
  created_at: string;
  scheduled_for: string;
  started_at: string | null;
  finished_at: string | null;
  task_id: string;
  owner_user_id: string;
  report_type: string;
  subject: string;
  title: string;
  filename: string;
  status: string;
  stage: string;
  session_id: string | null;
  thread_id: string | null;
  model: string;
  reasoning_effort: string;
  duration_seconds: number | null;
  report_id: string | null;
  error: string | null;
};

type ReportRunDetail = ReportRun & {
  markdown: string | null;
};

const ALL_REPORTS = "__all__";

const STATUS_LABEL: Record<string, string> = {
  queued: "等待中",
  running: "生成中",
  succeeded: "已完成",
  failed: "失败",
};

async function requestJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { cache: "no-store", signal });
  if (response.ok) return response.json() as Promise<T>;
  const body = (await response.json().catch(() => null)) as {
    detail?: string;
  } | null;
  throw new Error(body?.detail ?? `请求失败：${response.status}`);
}

function filterKey(reportType: string, subject: string): string {
  return JSON.stringify([reportType, subject]);
}

function localDateKey(value: Date): string {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function dateTime(value: string | null): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function duration(value: number | null): string {
  if (value == null) return "—";
  const seconds = Math.max(0, Math.round(value));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  if (hours) return `${hours}时${minutes}分${remainder}秒`;
  if (minutes) return `${minutes}分${remainder}秒`;
  return `${remainder}秒`;
}

function RunStatus({ status }: { status: string }) {
  return (
    <span className={`run-status run-status-${status}`}>
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

function ReportRunsTable({
  runs,
  onOpen,
}: {
  runs: ReportRun[];
  onOpen: (runId: string) => void;
}) {
  const [sorting, setSorting] = useState<SortingState>([
    { id: "scheduled_for", desc: true },
  ]);
  const columns = useMemo<ColumnDef<ReportRun>[]>(
    () => [
      {
        accessorKey: "scheduled_for",
        header: "计划时间",
        size: 190,
        sortingFn: (left, right) =>
          new Date(left.original.scheduled_for).getTime() -
          new Date(right.original.scheduled_for).getTime(),
        cell: (info) => dateTime(info.getValue<string>()),
      },
      {
        id: "report",
        accessorFn: (run) => run.title,
        header: "报告",
        size: 320,
        cell: (info) => {
          const run = info.row.original;
          return (
            <div>
              <div className="report-table-title">{run.title}</div>
              <div className="report-table-subtitle">
                {run.report_type} · {run.subject}
              </div>
            </div>
          );
        },
      },
      {
        accessorKey: "status",
        header: "状态",
        size: 110,
        cell: (info) => <RunStatus status={info.getValue<string>()} />,
      },
      {
        accessorKey: "duration_seconds",
        header: "耗时",
        size: 120,
        sortUndefined: "last",
        cell: (info) => duration(info.getValue<number | null>()),
      },
      {
        id: "model",
        accessorFn: (run) => `${run.model} · ${run.reasoning_effort}`,
        header: "模型 / 推理",
        size: 210,
      },
      {
        id: "trace",
        header: "Session / Thread",
        size: 250,
        enableSorting: false,
        cell: (info) => {
          const run = info.row.original;
          return (
            <div className="report-trace">
              <span title={run.session_id ?? undefined}>
                S · {run.session_id ?? "—"}
              </span>
              <span title={run.thread_id ?? undefined}>
                T · {run.thread_id ?? "—"}
              </span>
            </div>
          );
        },
      },
    ],
    [],
  );
  const table = useReactTable({
    data: runs,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize: 20 } },
  });

  return (
    <div className="report-table-shell">
      <div className="report-table-scroll">
        <table className="report-table">
          <thead>
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => {
                  const sorted = header.column.getIsSorted();
                  return (
                    <th key={header.id} style={{ width: header.getSize() }}>
                      {header.isPlaceholder ? null : (
                        <button
                          type="button"
                          className="report-table-sort"
                          onClick={header.column.getToggleSortingHandler()}
                          disabled={!header.column.getCanSort()}
                        >
                          {flexRender(
                            header.column.columnDef.header,
                            header.getContext(),
                          )}
                          {header.column.getCanSort() ? (
                            sorted === "asc" ? (
                              <ArrowUp aria-hidden="true" />
                            ) : sorted === "desc" ? (
                              <ArrowDown aria-hidden="true" />
                            ) : (
                              <ArrowUpDown aria-hidden="true" />
                            )
                          ) : null}
                        </button>
                      )}
                    </th>
                  );
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => (
              <tr
                key={row.id}
                className="report-table-row"
                onClick={() => onOpen(row.original.id)}
              >
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} style={{ width: cell.column.getSize() }}>
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
            {table.getRowModel().rows.length === 0 ? (
              <tr>
                <td colSpan={columns.length} className="report-table-empty">
                  暂无报告运行
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <div className="report-table-pagination">
        <span>共 {runs.length} 次运行</span>
        <label>
          每页
          <select
            value={table.getState().pagination.pageSize}
            onChange={(event) => table.setPageSize(Number(event.target.value))}
          >
            {[20, 50, 100].map((pageSize) => (
              <option key={pageSize} value={pageSize}>{pageSize}</option>
            ))}
          </select>
        </label>
        <button
          type="button"
          onClick={() => table.previousPage()}
          disabled={!table.getCanPreviousPage()}
          aria-label="上一页"
        >
          <ChevronLeft aria-hidden="true" />
        </button>
        <span>
          {table.getState().pagination.pageIndex + 1} / {table.getPageCount() || 1}
        </span>
        <button
          type="button"
          onClick={() => table.nextPage()}
          disabled={!table.getCanNextPage()}
          aria-label="下一页"
        >
          <ChevronRight aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}

function ReportDetail({
  run,
  loading,
  error,
  onBack,
}: {
  run: ReportRunDetail | null;
  loading: boolean;
  error: string | null;
  onBack: () => void;
}) {
  return (
    <main className="report-detail-shell">
      <section className="report-detail-layout">
        <div className="report-document-scroll">
          <div className="report-detail-back-row">
            <button type="button" onClick={onBack} className="report-back-button">
              <ArrowLeft aria-hidden="true" />
              返回
            </button>
          </div>

          <article className="report-document">
            {run ? (
              <div className="report-document-meta">
                <span>{run.report_type} · {run.subject}</span>
                <span>计划于 {dateTime(run.scheduled_for)}</span>
                <RunStatus status={run.status} />
              </div>
            ) : null}

            {loading ? <p className="report-state">正在读取报告…</p> : null}
            {error ? <p className="report-state report-state-error">{error}</p> : null}
            {run?.markdown ? (
              <div className="markdown-body">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {run.markdown}
                </ReactMarkdown>
              </div>
            ) : null}
            {run && !run.markdown && !loading ? (
              <div className="report-empty-state">
                <h1>{run.title}</h1>
                <p>
                  {run.status === "failed"
                    ? run.error ?? "这次报告生成失败。"
                    : "报告尚未生成完成。"}
                </p>
              </div>
            ) : null}
          </article>
        </div>

        <aside className="report-thread-pane">
          {run?.session_id && run.thread_id ? (
            <ThreadPanel
              sessionId={run.session_id}
              threadId={run.thread_id}
              model={run.model}
              reasoningEffort={run.reasoning_effort}
              live={run.status === "queued" || run.status === "running"}
            />
          ) : (
            <div className="thread-panel-state">
              {loading ? "正在读取生成线程…" : "生成线程尚未创建。"}
            </div>
          )}
        </aside>
      </section>
    </main>
  );
}

export function App() {
  const [viewMode, setViewMode] = useState<ViewMode>("calendar");
  const [filter, setFilter] = useState(ALL_REPORTS);
  const [calendarDate, setCalendarDate] = useState(
    () => localDateKey(new Date()),
  );
  const [tasks, setTasks] = useState<ReportTask[]>([]);
  const [runs, setRuns] = useState<ReportRun[]>([]);
  const [collectionError, setCollectionError] = useState<string | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [activeRun, setActiveRun] = useState<ReportRunDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const loadCollections = useCallback(async (signal?: AbortSignal) => {
    try {
      const [nextTasks, nextRuns] = await Promise.all([
        requestJson<ReportTask[]>("/api/report-tasks", signal),
        requestJson<ReportRun[]>("/api/report-runs", signal),
      ]);
      setTasks(nextTasks);
      setRuns(nextRuns);
      setCollectionError(null);
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") return;
      setCollectionError(
        cause instanceof Error ? cause.message : "报告运行读取失败",
      );
    }
  }, []);

  const loadDetail = useCallback(
    async (runId: string, signal?: AbortSignal) => {
      setDetailLoading(true);
      try {
        const nextRun = await requestJson<ReportRunDetail>(
          `/api/report-runs/${encodeURIComponent(runId)}`,
          signal,
        );
        setActiveRun(nextRun);
        setDetailError(null);
      } catch (cause) {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setDetailError(cause instanceof Error ? cause.message : "报告读取失败");
      } finally {
        if (!signal?.aborted) setDetailLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    const controller = new AbortController();
    void loadCollections(controller.signal);
    const events = new EventSource("/api/report-runs/events");
    events.addEventListener("changed", () => {
      void loadCollections(controller.signal);
      if (activeRunId) void loadDetail(activeRunId, controller.signal);
    });
    return () => {
      controller.abort();
      events.close();
    };
  }, [activeRunId, loadCollections, loadDetail]);

  const options = useMemo(() => {
    const combinations = new Map<string, string>();
    for (const task of tasks) {
      combinations.set(
        filterKey(task.report_type, task.subject),
        `${task.report_type} · ${task.subject}`,
      );
    }
    for (const run of runs) {
      combinations.set(
        filterKey(run.report_type, run.subject),
        `${run.report_type} · ${run.subject}`,
      );
    }
    return [
      { value: ALL_REPORTS, label: "全部报告" },
      ...[...combinations.entries()]
        .sort((left, right) => left[1].localeCompare(right[1], "zh-CN"))
        .map(([value, label]) => ({ value, label })),
    ];
  }, [runs, tasks]);

  const filteredRuns = useMemo(
    () =>
      filter === ALL_REPORTS
        ? runs
        : runs.filter(
            (run) => filterKey(run.report_type, run.subject) === filter,
          ),
    [filter, runs],
  );

  const openRun = useCallback(
    (runId: string) => {
      setActiveRunId(runId);
      setActiveRun(null);
      setDetailError(null);
      void loadDetail(runId);
    },
    [loadDetail],
  );

  if (activeRunId) {
    return (
      <ReportDetail
        run={activeRun}
        loading={detailLoading}
        error={detailError}
        onBack={() => {
          setActiveRunId(null);
          setActiveRun(null);
          setDetailError(null);
        }}
      />
    );
  }

  const calendarEvents = filteredRuns.map((run) => ({
    id: run.id,
    title: run.title,
    start: run.scheduled_for,
    display: "list-item",
    backgroundColor: run.status === "failed" ? "#f6dedb" : "#e2eee4",
    borderColor: run.status === "failed" ? "#e9bbb5" : "#c5dbc9",
    textColor: run.status === "failed" ? "#8f3028" : "#244d35",
  }));

  return (
      <main className="report-workspace">
        <section className="report-collection">
          <div className="report-toolbar">
            <select
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              className="report-filter"
              aria-label="按报告类型和主题筛选"
            >
              {options.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <div className="report-view-switch" aria-label="报告视图">
              <button
                type="button"
                className={viewMode === "calendar" ? "is-active" : ""}
                onClick={() => setViewMode("calendar")}
                aria-label="日历视图"
                title="日历视图"
              >
                <CalendarDays aria-hidden="true" />
              </button>
              <button
                type="button"
                className={viewMode === "table" ? "is-active" : ""}
                onClick={() => setViewMode("table")}
                aria-label="表格视图"
                title="表格视图"
              >
                <TableProperties aria-hidden="true" />
              </button>
            </div>
          </div>

          {collectionError ? (
            <div className="collection-error">{collectionError}</div>
          ) : null}

          <div className="report-view-surface">
            {viewMode === "calendar" ? (
              <div className="report-calendar">
                <EventCalendar
                  availableViews={["dayGridMonth"]}
                  initialDate={calendarDate}
                  locale={zhCnLocale}
                  firstDay={1}
                  events={calendarEvents}
                  defaultTimedEventDuration={{ milliseconds: 1 }}
                  eventDisplay="list-item"
                  eventClick={(info) => openRun(info.event.id)}
                  datesSet={(info) =>
                    setCalendarDate(localDateKey(info.view.currentStart))
                  }
                  dayMaxEvents
                  borderless
                  height="auto"
                />
              </div>
            ) : (
              <ReportRunsTable runs={filteredRuns} onOpen={openRun} />
            )}
          </div>
        </section>
      </main>
  );
}
