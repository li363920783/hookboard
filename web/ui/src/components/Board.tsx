import { ApprovalForm } from "@/components/ApprovalForm";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { elapsed } from "@/lib/format";
import type { Column, Task } from "@/lib/types";

export const COLUMNS: { key: string; title: string; empty: string; dot: string }[] = [
  { key: "pending", title: "待处理", empty: "暂无任务", dot: "bg-[var(--status-pending)]" },
  { key: "running", title: "进行中", empty: "没有正在执行的任务", dot: "bg-[var(--status-running)]" },
  { key: "waiting", title: "等你审批", empty: "没有待审批项", dot: "bg-[var(--status-waiting)]" },
  { key: "done", title: "已完成", empty: "还没有已完成任务", dot: "bg-[var(--status-done)]" },
  { key: "failed", title: "失败", empty: "没有失败任务", dot: "bg-[var(--status-failed)]" },
];

const STATUS_BAR: Record<string, string> = {
  pending: "bg-[var(--status-pending)]",
  running: "bg-[var(--status-running)]",
  waiting: "bg-[var(--status-waiting)]",
  done: "bg-[var(--status-done)]",
  failed: "bg-[var(--status-failed)]",
  cancelled: "bg-[var(--status-failed)]",
};

const ACTIVE = ["pending", "running", "waiting"];

export interface BoardProps {
  columns: Column[];
  loading: boolean;
  onOpen: (task: Task) => void;
  onDecide: (token: string, payload: { index?: number; text?: string }) => Promise<boolean>;
}

export function Board({ columns, loading, onOpen, onDecide }: BoardProps) {
  const byKey = new Map(columns.map((column) => [column.key, column.tasks]));
  return (
    <main className="grid grid-cols-1 gap-3 px-4 py-3 md:grid-cols-2 xl:min-h-0 xl:flex-1 xl:grid-cols-5 xl:overflow-hidden">
      {COLUMNS.map((column) => {
        const tasks = byKey.get(column.key) || [];
        return (
          <section
            key={column.key}
            className="bg-muted/20 flex min-h-0 flex-col rounded-xl border p-1.5"
          >
            <header className="flex items-center gap-2 px-1.5 pt-1 pb-2">
              <span className={cn("size-1.5 shrink-0 rounded-full", column.dot)} />
              <h2 className="text-[13px] font-medium">{column.title}</h2>
              <span className="text-muted-foreground ml-auto font-mono text-[11px] tabular-nums">
                {tasks.length}
              </span>
            </header>
            <div className="flex min-h-0 flex-col gap-2 px-0.5 pb-1 xl:flex-1 xl:overflow-y-auto">
              {loading && columns.length === 0 ? (
                <>
                  <Skeleton className="h-20 rounded-lg" />
                  <Skeleton className="h-16 rounded-lg" />
                </>
              ) : tasks.length === 0 ? (
                <p className="text-muted-foreground m-0.5 rounded-lg border border-dashed px-3 py-6 text-center text-xs">
                  {column.empty}
                </p>
              ) : (
                tasks.map((task) => (
                  <TaskCard key={task.id} task={task} onOpen={onOpen} onDecide={onDecide} />
                ))
              )}
            </div>
          </section>
        );
      })}
    </main>
  );
}

function TaskCard({
  task,
  onOpen,
  onDecide,
}: {
  task: Task;
  onOpen: (task: Task) => void;
  onDecide: BoardProps["onDecide"];
}) {
  return (
    <Card
      onClick={() => onOpen(task)}
      className="hover:bg-accent/40 relative cursor-pointer gap-2 overflow-hidden py-2.5 pr-2.5 pl-3 transition-colors"
    >
      <span
        aria-hidden
        className={cn("absolute inset-y-0 left-0 w-0.5 opacity-80", STATUS_BAR[task.status] || "bg-border")}
      />
      <div className="flex items-center gap-2 text-[11px]">
        <span className="text-muted-foreground font-mono tracking-wider uppercase">{task.source}</span>
        <span className="text-muted-foreground/70 ml-auto max-w-[62%] truncate font-mono">
          {task.external_id}
        </span>
      </div>
      <button
        type="button"
        onClick={(event) => { event.stopPropagation(); onOpen(task); }}
        className="focus-visible:ring-ring/50 -m-1 rounded p-1 text-left text-sm leading-snug font-medium focus-visible:ring-[3px] focus-visible:outline-none"
      >
        {task.title}
      </button>
      <div className="text-muted-foreground flex flex-wrap items-center gap-x-1.5 gap-y-1 text-[11px]">
        <Badge variant="outline" className="font-mono">
          {task.stage}
        </Badge>
        <span aria-hidden>·</span>
        <time className="tabular-nums">{elapsed(task.created_at)}</time>
        {task.session && ACTIVE.includes(task.status) ? (
          <Badge variant="outline" className="text-foreground ml-auto shrink-0 border-ring/50">
            已派单
          </Badge>
        ) : null}
      </div>
      {task.note ? (
        <p className="text-muted-foreground m-0 line-clamp-2 text-xs leading-relaxed">{task.note}</p>
      ) : null}
      {task.approval ? (
        <div
          className="mt-0.5 cursor-default rounded-md border p-2"
          style={{
            borderColor: "color-mix(in oklab, var(--status-waiting) 32%, transparent)",
            background: "color-mix(in oklab, var(--status-waiting) 7%, transparent)",
          }}
          onClick={(event) => event.stopPropagation()}
          onKeyDown={(event) => event.stopPropagation()}
        >
          <ApprovalForm key={task.approval.token} approval={task.approval} onDecide={onDecide} />
        </div>
      ) : null}
    </Card>
  );
}