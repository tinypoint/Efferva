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
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
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

const STATUS_STYLE: Record<string, string> = {
  queued: "bg-[#edf4ee] text-[#24603c]",
  running: "bg-[#edf4ee] text-[#24603c]",
  succeeded: "bg-[#e5f2e8] text-[#24603c]",
  failed: "bg-[#fbeceb] text-[#a6382d]",
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
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-1 text-[0.68rem] font-semibold",
        STATUS_STYLE[status] ?? "bg-muted text-muted-foreground",
      )}
    >
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
              <div className="text-[0.82rem] font-semibold text-[#172018]">
                {run.title}
              </div>
              <div className="mt-0.5 text-[0.7rem] text-[#8a9189]">
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
            <div className="grid gap-0.5 font-mono text-[0.65rem] text-[#697169] [&>span]:overflow-hidden [&>span]:text-ellipsis [&>span]:whitespace-nowrap">
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
    <div className="min-w-0">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[75rem] table-fixed border-collapse text-xs text-[#354038] [&_tbody_tr:last-child_td]:border-b-0">
          <thead>
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => {
                  const sorted = header.column.getIsSorted();
                  return (
                    <th
                      key={header.id}
                      className="border-b border-[#deddd5] bg-[#f5f4ee] p-0 text-left text-[0.68rem] font-semibold text-[#747a72]"
                      style={{ width: header.getSize() }}
                    >
                      {header.isPlaceholder ? null : (
                        <button
                          type="button"
                          className="flex min-h-11 w-full items-center justify-between gap-2 border-0 bg-transparent px-3.5 py-3 text-left font-[inherit] text-[inherit] enabled:hover:bg-[#edebe2] disabled:cursor-default [&_svg]:size-3 [&_svg]:shrink-0"
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
                className="cursor-pointer hover:bg-[#fafaf7]"
                onClick={() => onOpen(row.original.id)}
              >
                {row.getVisibleCells().map((cell) => (
                  <td
                    key={cell.id}
                    className="h-[3.7rem] border-b border-[#e8e6df] px-3.5 py-3 align-middle"
                    style={{ width: cell.column.getSize() }}
                  >
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
            {table.getRowModel().rows.length === 0 ? (
              <tr>
                <td
                  colSpan={columns.length}
                  className="h-48 text-center text-[#8a9189]"
                >
                  暂无报告运行
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <div className="flex min-h-14 items-center justify-end gap-2.5 border-t border-[#deddd5] px-3.5 py-2.5 text-[0.7rem] text-[#747a72]">
        <span>共 {runs.length} 次运行</span>
        <label className="flex items-center gap-1.5">
          每页
          <select
            className="h-8 rounded-md border border-[#deddd5] bg-white px-2 font-[inherit] text-[#475148]"
            value={table.getState().pagination.pageSize}
            onChange={(event) => table.setPageSize(Number(event.target.value))}
          >
            {[20, 50, 100].map((pageSize) => (
              <option key={pageSize} value={pageSize}>{pageSize}</option>
            ))}
          </select>
        </label>
        <Button
          type="button"
          variant="outline"
          size="icon"
          onClick={() => table.previousPage()}
          disabled={!table.getCanPreviousPage()}
          aria-label="上一页"
        >
          <ChevronLeft aria-hidden="true" className="size-3.5" />
        </Button>
        <span>
          {table.getState().pagination.pageIndex + 1} / {table.getPageCount() || 1}
        </span>
        <Button
          type="button"
          variant="outline"
          size="icon"
          onClick={() => table.nextPage()}
          disabled={!table.getCanNextPage()}
          aria-label="下一页"
        >
          <ChevronRight aria-hidden="true" className="size-3.5" />
        </Button>
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
    <main className="h-screen min-h-0 overflow-hidden bg-[#f8f7f2] text-[#172018]">
      <section className="grid h-full overflow-hidden [grid-template-columns:minmax(0,3fr)_minmax(25rem,2fr)] max-[880px]:block max-[880px]:overflow-y-auto">
        <div className="min-w-0 overflow-y-auto p-8 max-[880px]:overflow-visible max-[880px]:p-4">
          <div className="mx-auto mb-3 w-full max-w-[60rem]">
            <Button type="button" onClick={onBack} variant="ghost">
              <ArrowLeft aria-hidden="true" className="size-4" />
              返回
            </Button>
          </div>

          <article className="mx-auto min-h-[calc(100vh-7rem)] w-full max-w-[60rem] rounded-xl border border-[#deddd5] bg-white p-10 shadow-[0_1px_2px_rgb(23_32_24/4%)] max-[880px]:min-h-0 max-[880px]:p-6">
            {run ? (
              <div className="mb-9 flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-[#e6e4dc] pb-4 text-xs text-[#747a72]">
                <span>{run.report_type} · {run.subject}</span>
                <span>计划于 {dateTime(run.scheduled_for)}</span>
                <RunStatus status={run.status} />
              </div>
            ) : null}

            {loading ? (
              <p className="text-sm text-[#747a72]">正在读取报告…</p>
            ) : null}
            {error ? <p className="text-sm text-[#a6382d]">{error}</p> : null}
            {run?.markdown ? (
              <div className="rich-text">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {run.markdown}
                </ReactMarkdown>
              </div>
            ) : null}
            {run && !run.markdown && !loading ? (
              <div className="text-sm text-[#747a72]">
                <h1 className="mb-4 text-3xl leading-tight font-semibold text-[#172018]">
                  {run.title}
                </h1>
                <p>
                  {run.status === "failed"
                    ? run.error ?? "这次报告生成失败。"
                    : "报告尚未生成完成。"}
                </p>
              </div>
            ) : null}
          </article>
        </div>

        <aside className="min-h-0 min-w-0 border-l border-[#deddd5] bg-white max-[880px]:h-[70vh] max-[880px]:min-h-[36rem] max-[880px]:border-t max-[880px]:border-l-0">
          {run?.session_id && run.thread_id ? (
            <ThreadPanel
              sessionId={run.session_id}
              threadId={run.thread_id}
              model={run.model}
              reasoningEffort={run.reasoning_effort}
              live={run.status === "queued" || run.status === "running"}
            />
          ) : (
            <div className="flex h-full min-h-40 items-center justify-center p-8 text-center text-sm text-[#747a72]">
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
      <main className="min-h-screen bg-[#f8f7f2] text-[#172018]">
        <section className="mx-auto w-full max-w-[96rem] p-6 max-[880px]:p-3">
          <div className="mb-3.5 flex items-center gap-2">
            <select
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              className="h-10 w-[min(22rem,calc(100vw-8rem))] rounded-lg border border-[#deddd5] bg-white px-3 pr-8 text-sm text-[#172018] outline-none focus:border-[#8fa697] focus:ring-2 focus:ring-[#244d35]/10"
              aria-label="按报告类型和主题筛选"
            >
              {options.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <div
              className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-[#deddd5] bg-white p-1"
              aria-label="报告视图"
            >
              <Button
                type="button"
                variant={viewMode === "calendar" ? "default" : "ghost"}
                size="icon"
                onClick={() => setViewMode("calendar")}
                aria-label="日历视图"
                title="日历视图"
              >
                <CalendarDays aria-hidden="true" className="size-4" />
              </Button>
              <Button
                type="button"
                variant={viewMode === "table" ? "default" : "ghost"}
                size="icon"
                onClick={() => setViewMode("table")}
                aria-label="表格视图"
                title="表格视图"
              >
                <TableProperties aria-hidden="true" className="size-4" />
              </Button>
            </div>
          </div>

          {collectionError ? (
            <div className="mb-3.5 rounded-lg border border-[#efcbc7] bg-[#fff3f1] px-3.5 py-3 text-sm text-[#a6382d]">
              {collectionError}
            </div>
          ) : null}

          <div className="overflow-hidden rounded-xl border border-[#deddd5] bg-white max-[880px]:overflow-x-auto">
            {viewMode === "calendar" ? (
              <div className="p-5 text-sm text-[#172018] max-[880px]:min-w-[46rem]">
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
