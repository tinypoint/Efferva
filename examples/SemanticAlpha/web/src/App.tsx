import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, ChevronLeft, ChevronRight } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { ThreadPanel } from "./ThreadPanel";

const WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

type IndustryReportSummary = {
  id: string;
  created_at: string;
  title: string;
};

type IndustryReport = {
  id: string;
  created_at: string;
  session_id: string | null;
  thread_id: string | null;
  model: string | null;
  reasoning_effort: string | null;
  markdown: string;
};

function sameDay(left: Date, right: Date) {
  return (
    left.getFullYear() === right.getFullYear() &&
    left.getMonth() === right.getMonth() &&
    left.getDate() === right.getDate()
  );
}

function monthDays(month: Date) {
  const first = new Date(month.getFullYear(), month.getMonth(), 1);
  const offset = (first.getDay() + 6) % 7;
  const start = new Date(first.getFullYear(), first.getMonth(), 1 - offset);

  return Array.from({ length: 42 }, (_, index) =>
    new Date(start.getFullYear(), start.getMonth(), start.getDate() + index),
  );
}

function monthLabel(month: Date) {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "long",
  }).format(month);
}

function reportTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function reportDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "long",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

async function requestJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal });
  if (response.ok) return response.json() as Promise<T>;
  const body = (await response.json().catch(() => null)) as {
    detail?: string;
  } | null;
  throw new Error(body?.detail ?? `请求失败：${response.status}`);
}

export function App() {
  const today = useMemo(() => new Date(), []);
  const [month, setMonth] = useState(
    () => new Date(today.getFullYear(), today.getMonth(), 1),
  );
  const [reports, setReports] = useState<IndustryReportSummary[]>([]);
  const [listError, setListError] = useState<string | null>(null);
  const [activeSummary, setActiveSummary] =
    useState<IndustryReportSummary | null>(null);
  const [activeReport, setActiveReport] = useState<IndustryReport | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const days = useMemo(() => monthDays(month), [month]);

  useEffect(() => {
    const controller = new AbortController();
    requestJson<IndustryReportSummary[]>(
      "/api/industry-reports",
      controller.signal,
    )
      .then(setReports)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setListError(error instanceof Error ? error.message : "报告读取失败");
      });
    return () => controller.abort();
  }, []);

  const moveMonth = (offset: number) => {
    setMonth(new Date(month.getFullYear(), month.getMonth() + offset, 1));
  };

  const goToday = () => {
    setMonth(new Date(today.getFullYear(), today.getMonth(), 1));
  };

  const openReport = async (summary: IndustryReportSummary) => {
    setActiveSummary(summary);
    setActiveReport(null);
    setReportError(null);
    try {
      const report = await requestJson<IndustryReport>(
        `/api/industry-reports/${summary.id}`,
      );
      setActiveReport(report);
    } catch (error) {
      setReportError(error instanceof Error ? error.message : "报告读取失败");
    }
  };

  const closeReport = () => {
    setActiveSummary(null);
    setActiveReport(null);
    setReportError(null);
  };

  if (activeSummary) {
    return (
      <main className="flex h-screen min-h-0 flex-col overflow-hidden bg-canvas text-ink">
        <header className="z-10 flex shrink-0 items-center justify-between border-b border-line bg-canvas/95 px-5 py-4 backdrop-blur sm:px-8">
          <button
            type="button"
            onClick={closeReport}
            className="flex items-center gap-2 rounded-lg px-2 py-2 text-sm font-medium hover:bg-panel"
          >
            <ArrowLeft className="size-4" />
            返回日历
          </button>
          <span className="text-sm font-semibold tracking-[-0.02em]">
            Semantic Alpha
          </span>
        </header>

        <section className="report-detail-layout min-h-0 flex-1">
          <div className="report-document-scroll">
            <article className="mx-auto max-w-[960px] rounded-xl border border-line bg-white px-5 py-8 shadow-sm sm:px-10 sm:py-12">
              <div className="mb-8 border-b border-line pb-5 text-sm text-muted">
                {reportDate(activeSummary.created_at)}
              </div>
              {!activeReport && !reportError ? (
                <p className="text-sm text-muted">正在读取报告…</p>
              ) : null}
              {reportError ? (
                <p className="text-sm text-red-700">{reportError}</p>
              ) : null}
              {activeReport ? (
                <div className="markdown-body">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {activeReport.markdown}
                  </ReactMarkdown>
                </div>
              ) : null}
            </article>
          </div>

          <aside className="report-thread-pane">
            {!activeReport && !reportError ? (
              <div className="thread-panel-state">正在读取来源关系…</div>
            ) : null}
            {activeReport?.session_id && activeReport.thread_id ? (
              <ThreadPanel
                sessionId={activeReport.session_id}
                threadId={activeReport.thread_id}
                model={activeReport.model}
                reasoningEffort={activeReport.reasoning_effort}
              />
            ) : null}
            {activeReport &&
            (!activeReport.session_id || !activeReport.thread_id) ? (
              <div className="thread-panel-state">
                这份历史报告没有关联生成线程。
              </div>
            ) : null}
          </aside>
        </section>
      </main>
    );
  }

  return (
    <main className="flex min-h-screen flex-col bg-canvas text-ink">
      <header className="flex flex-col justify-between gap-4 border-b border-line px-5 py-5 sm:flex-row sm:items-center sm:px-8">
        <div>
          <div className="text-sm font-semibold tracking-[-0.02em]">
            Semantic Alpha
          </div>
          <h1 className="mt-1 text-2xl font-semibold tracking-[-0.04em]">
            产业调查日历
          </h1>
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={goToday}
            className="rounded-lg border border-line bg-white px-3 py-2 text-sm font-medium hover:bg-panel"
          >
            今天
          </button>
          <div className="flex items-center rounded-lg border border-line bg-white p-1">
            <button
              type="button"
              onClick={() => moveMonth(-1)}
              className="grid size-8 place-items-center rounded-md text-muted hover:bg-panel hover:text-ink"
              aria-label="上个月"
            >
              <ChevronLeft className="size-4" />
            </button>
            <span className="min-w-32 px-3 text-center text-sm font-semibold">
              {monthLabel(month)}
            </span>
            <button
              type="button"
              onClick={() => moveMonth(1)}
              className="grid size-8 place-items-center rounded-md text-muted hover:bg-panel hover:text-ink"
              aria-label="下个月"
            >
              <ChevronRight className="size-4" />
            </button>
          </div>
        </div>
      </header>

      <section className="min-h-0 flex-1 overflow-auto p-4 sm:p-8">
        {listError ? (
          <p className="mx-auto mb-3 max-w-[1500px] text-sm text-red-700">
            {listError}
          </p>
        ) : null}
        <div className="mx-auto min-w-[760px] max-w-[1500px] overflow-hidden rounded-xl border border-line bg-white">
          <div className="grid grid-cols-7 border-b border-line bg-panel/60">
            {WEEKDAYS.map((day, index) => (
              <div
                key={day}
                className={`px-3 py-3 text-xs font-medium ${
                  index > 4 ? "text-amber-700" : "text-muted"
                }`}
              >
                {day}
              </div>
            ))}
          </div>

          <div className="grid grid-cols-7">
            {days.map((date) => {
              const inMonth = date.getMonth() === month.getMonth();
              const isToday = sameDay(date, today);
              const dayReports = reports.filter((report) =>
                sameDay(new Date(report.created_at), date),
              );

              return (
                <div
                  key={date.toISOString()}
                  className={`min-h-28 border-r border-b border-line p-2.5 ${
                    inMonth ? "bg-white" : "bg-canvas/70 text-muted/45"
                  }`}
                >
                  <span
                    className={`grid size-7 place-items-center rounded-full text-xs font-medium ${
                      isToday ? "bg-brand text-white" : ""
                    }`}
                  >
                    {date.getDate()}
                  </span>

                  <div className="mt-2 space-y-1">
                    {dayReports.map((report) => (
                      <button
                        key={report.id}
                        type="button"
                        onClick={() => void openReport(report)}
                        className="block w-full rounded-md bg-[#edf4ee] px-2 py-1.5 text-left text-xs text-ink transition hover:bg-[#dfeadf]"
                      >
                        <span className="mr-1.5 text-muted">
                          {reportTime(report.created_at)}
                        </span>
                        <span className="font-medium">{report.title}</span>
                      </button>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </section>
    </main>
  );
}
