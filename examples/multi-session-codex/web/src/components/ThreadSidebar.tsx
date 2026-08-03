import { Menu } from "@base-ui/react/menu";
import { Code2, MoreHorizontal, Plus, Trash2 } from "lucide-react";

import type { ThreadSummary } from "../types";

type ThreadSidebarProps = {
  threads: ThreadSummary[];
  selectedThreadId?: string;
  onNewThread: () => void;
  onSelectThread: (threadId: string) => void;
  onDeleteThread: (thread: ThreadSummary) => void;
};

function threadTitle(thread: ThreadSummary): string {
  return thread.name?.trim() || "Untitled thread";
}

export function ThreadSidebar({
  threads,
  selectedThreadId,
  onNewThread,
  onSelectThread,
  onDeleteThread,
}: ThreadSidebarProps) {
  return (
    <aside className="flex min-h-0 flex-col border-r bg-sidebar p-3 max-md:hidden">
      <div className="mb-4 flex items-center gap-2 px-2 py-1">
        <span className="grid size-8 place-items-center rounded-lg bg-primary text-primary-foreground">
          <Code2 className="size-4" />
        </span>
        <div>
          <div className="text-sm font-semibold">Efferva</div>
          <div className="text-xs text-muted-foreground">
            Local coding agent
          </div>
        </div>
      </div>
      <button
        type="button"
        className="mb-3 flex items-center gap-2 rounded-lg px-2 py-2 text-sm font-medium hover:bg-sidebar-accent"
        onClick={onNewThread}
      >
        <Plus className="size-4" />
        New Thread
      </button>
      <div className="min-h-0 flex-1 space-y-1 overflow-y-auto">
        {threads.map((thread) => (
          <div
            key={thread.id}
            className={`group flex items-center rounded-lg ${
              thread.id === selectedThreadId
                ? "bg-sidebar-accent"
                : "hover:bg-sidebar-accent/60"
            }`}
          >
            <button
              type="button"
              className="min-w-0 flex-1 truncate px-3 py-2 text-left text-sm"
              onClick={() => onSelectThread(thread.id)}
            >
              {threadTitle(thread)}
            </button>
            <Menu.Root>
              <Menu.Trigger
                className="mr-1 rounded p-1 opacity-0 outline-none hover:bg-background focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring group-hover:opacity-100 data-popup-open:bg-background data-popup-open:opacity-100"
                aria-label={`Actions for ${threadTitle(thread)}`}
              >
                <MoreHorizontal className="size-4" />
              </Menu.Trigger>
              <Menu.Portal>
                <Menu.Positioner
                  side="bottom"
                  align="end"
                  sideOffset={4}
                  className="z-50"
                >
                  <Menu.Popup
                    finalFocus={false}
                    className="min-w-32 rounded-lg bg-popover p-1 text-sm text-popover-foreground shadow-md ring-1 ring-foreground/10 outline-none data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95"
                  >
                    <Menu.Item
                      className="flex cursor-default items-center gap-2 rounded-md px-2 py-1.5 text-destructive outline-none data-highlighted:bg-destructive/10"
                      onClick={() => onDeleteThread(thread)}
                    >
                      <Trash2 className="size-4" />
                      Delete
                    </Menu.Item>
                  </Menu.Popup>
                </Menu.Positioner>
              </Menu.Portal>
            </Menu.Root>
          </div>
        ))}
      </div>
    </aside>
  );
}
