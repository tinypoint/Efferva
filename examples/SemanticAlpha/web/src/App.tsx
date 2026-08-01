import { useMemo, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";

const WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

const CURRENT_MONTH_EVENTS = [
  { day: 4, time: "09:00", title: "公司财报" },
  { day: 8, time: "15:00", title: "组合复盘" },
  { day: 13, time: "20:30", title: "宏观数据" },
  { day: 20, time: "19:00", title: "行业跟踪" },
  { day: 27, time: "16:00", title: "月度复盘" },
];

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

export function App() {
  const today = useMemo(() => new Date(), []);
  const [month, setMonth] = useState(
    () => new Date(today.getFullYear(), today.getMonth(), 1),
  );
  const days = useMemo(() => monthDays(month), [month]);
  const showingCurrentMonth =
    month.getFullYear() === today.getFullYear() &&
    month.getMonth() === today.getMonth();

  const moveMonth = (offset: number) => {
    setMonth(new Date(month.getFullYear(), month.getMonth() + offset, 1));
  };

  const goToday = () => {
    setMonth(new Date(today.getFullYear(), today.getMonth(), 1));
  };

  return (
    <main className="flex min-h-screen flex-col bg-canvas text-ink">
      <header className="flex flex-col justify-between gap-4 border-b border-line px-5 py-5 sm:flex-row sm:items-center sm:px-8">
        <div>
          <div className="text-sm font-semibold tracking-[-0.02em]">
            Semantic Alpha
          </div>
          <h1 className="mt-1 text-2xl font-semibold tracking-[-0.04em]">
            投资日历
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
              const events =
                showingCurrentMonth && inMonth
                  ? CURRENT_MONTH_EVENTS.filter(
                      (event) => event.day === date.getDate(),
                    )
                  : [];

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
                    {events.map((event) => (
                      <div
                        key={event.title}
                        className="rounded-md bg-panel px-2 py-1.5 text-xs"
                      >
                        <span className="mr-1.5 text-muted">{event.time}</span>
                        <span className="font-medium">{event.title}</span>
                      </div>
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
