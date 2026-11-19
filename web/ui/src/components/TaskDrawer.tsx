import { useState } from "react";
import { X } from "lucide-react";
import { ApprovalForm } from "@/components/ApprovalForm";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { approvalAnswer, approvalBy, approvalState, clock, clockTime, deliveryRun, elapsed, kindLabel, mdInline, num, processEvents } from "@/lib/format";
import type { Approval, Detail, Run } from "@/lib/types";

const STATUS_COLOR: Record<string, string> = {
  pending: "var(--status-pending)",
  running: "var(--status-running)",
  waiting: "var(--status-waiting)",
  done: "var(--status-done)",
  failed: "var(--status-failed)",
  cancelled: "var(--status-failed)",
};

interface DrawerProps {
  open: boolean;
  detail: Detail | null;
  loading: boolean;
  onClose: () => void;
  onDecide: (token: string, payload: { index?: number; text?: string }) => Promise<boolean>;
  onMessage: (text: string) => Promise<boolean>;
  onCancel: () => void;
  onRetry: () => void;
  onWake: () => void;
  onReload: () => void;
}

export function TaskDrawer({ open, detail, loading, onClose, onDecide, onMessage, onCancel, onRetry, onWake, onReload }: DrawerProps) {
  const task = detail?.task;
  return (
    <Sheet open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <SheetContent className="gap-0 p-0">
        <div className="flex items-start gap-3 border-b px-5 py-4">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-1.5">
              {task ? (
                <>
                  <Badge
                    variant="outline"
                    className="font-mono"
                    style={{ color: STATUS_COLOR[task.status], borderColor: "color-mix(in oklab, currentColor 40%, transparent)" }}
                  >
                    {task.status}
                  </Badge>
                  <Badge variant="outline" className="font-mono">{task.stage}</Badge>
                  <Badge variant="outline">{task.source}</Badge>
                </>
              ) : (
                <Skeleton className="h-5 w-32" />
              )}
            </div>
            <SheetTitle className="mt-2 text-base leading-snug font-semibold break-words">
              {task ? task.title : "任务详情"}
            </SheetTitle>
            <SheetDescription className="sr-only">任务详情与操作</SheetDescription>
          </div>
          <SheetClose asChild>
            <Button variant="ghost" size="icon" aria-label="关闭">
              <X />
            </Button>
          </SheetClose>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 py-4">
          {!detail ? (
            loading ? (
              <div className="space-y-3">
                <Skeleton className="h-40 w-full" />
                <Skeleton className="h-24 w-full" />
                <Skeleton className="h-24 w-full" />
              </div>
            ) : (
              <div className="flex items-center gap-2 text-xs">
                <span className="text-muted-foreground">加载失败，请重试。</span>
                <Button size="sm" variant="outline" onClick={onReload}>重试</Button>
              </div>
            )
          ) : (
            <div className="space-y-5">
              <Section title="概览">
                <dl className="grid grid-cols-[90px_1fr] gap-x-3 gap-y-1.5 text-xs">
                  <Fact label="状态" value={`${task!.status} / ${task!.stage}`} />
                  <Fact label="项目" value={task!.project || "—"} />
                  <Fact label="派单目标" value={task!.workspace ? `${task!.workspace}${task!.branch ? ` @ ${task!.branch}` : ""}` : "—"} mono />
                  <Fact label="会话" value={task!.session || "—"} mono />
                  <Fact label="来源 / 类型" value={`${task!.source} · ${task!.kind || "—"}`} />
                  <Fact label="条目" value={task!.external_id} mono />
                  <Fact label="创建" value={`${clock(task!.created_at)}（${elapsed(task!.created_at)}）`} />
                  <Fact label="更新" value={clock(task!.updated_at)} />
                  <Fact label="尝试" value={String(num(task!.attempts))} />
                </dl>
                {task!.note ? <p className="text-muted-foreground mt-2.5 text-xs">{task!.note}</p> : null}
              </Section>

              {String(task!.context || "").trim() ? (
                <Section title="需求详情">
                  <div
                    className="bg-muted/20 max-h-[30vh] overflow-auto rounded-md border p-3 text-[13px] leading-relaxed whitespace-pre-wrap break-words"
                    dangerouslySetInnerHTML={{ __html: mdInline(String(task!.context)) }}
                  />
                </Section>
              ) : null}

              <DeliveryRun runs={detail.runs} />

              {detail.approvals?.filter((approval) => approval.status === "open").map((approval) => (
                <OpenApproval key={approval.token} approval={approval} onDecide={onDecide} />
              ))}

              {task!.status === "running" && task!.session ? <MessageBox onMessage={onMessage} /> : null}

              <ApprovalHistory approvals={detail.approvals || []} />

              <Section title="运行记录">
                <RunsList runs={detail.runs || []} />
              </Section>

              <ProcessList events={detail.events || []} />

              <Section title="原始数据">
                <details className="text-muted-foreground text-xs">
                  <summary className="hover:text-foreground cursor-pointer">报文 payload</summary>
                  <pre className="bg-muted/20 mt-2 max-h-[46vh] overflow-auto rounded-md border p-2.5 font-mono text-[11.5px] leading-relaxed break-all whitespace-pre-wrap">
                    {JSON.stringify(task!.payload || {}, null, 2)}
                  </pre>
                </details>
                <details className="text-muted-foreground mt-2 text-xs">
                  <summary className="hover:text-foreground cursor-pointer">
                    原始事件 {(detail.events || []).length} 条
                  </summary>
                  <pre className="bg-muted/20 mt-2 max-h-[46vh] overflow-auto rounded-md border p-2.5 font-mono text-[11.5px] leading-relaxed break-all whitespace-pre-wrap">
                    {(detail.events || []).slice(-300).map((event) => `${clock(event.ts)}  ${event.kind}  ${event.detail || ""}`).join("\n")}
                  </pre>
                </details>
              </Section>
            </div>
          )}
        </div>

        {task ? (
          <div className="flex justify-end gap-2 border-t px-5 py-3">
            {task.status === "failed" ? <Button size="sm" onClick={onRetry}>重试</Button> : null}
            {task.status === "pending" ? <Button size="sm" variant="outline" onClick={onWake}>唤醒</Button> : null}
            {["pending", "running", "waiting"].includes(task.status) ? (
              <Button size="sm" variant="destructive" onClick={onCancel}>取消任务</Button>
            ) : null}
          </div>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="text-muted-foreground mb-2 text-[11.5px] font-semibold tracking-wide">{title}</h3>
      {children}
    </section>
  );
}

function Fact({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className={`m-0 break-words ${mono ? "font-mono text-[11.5px]" : ""}`}>{value}</dd>
    </>
  );
}

function OpenApproval({ approval, onDecide }: { approval: Approval; onDecide: DrawerProps["onDecide"] }) {
  return (
    <Section title={`等你审批 · ${approval.stage || ""}`}>
      <ApprovalForm approval={approval} onDecide={onDecide} />
    </Section>
  );
}

function MessageBox({ onMessage }: { onMessage: (text: string) => Promise<boolean> }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <Section title="给运行中的会话发指令">
      <Textarea
        rows={3}
        name="message"
        placeholder="补充要求、纠正方向、让它先停下来（忙时会作为下一轮消息）"
        value={text}
        onChange={(event) => setText(event.target.value)}
      />
      <div className="mt-2 flex justify-end">
        <Button
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={async () => {
            if (!text.trim()) return;
            setBusy(true);
            try {
              const ok = await onMessage(text.trim());
              if (ok) setText("");
            } finally {
              setBusy(false);
            }
          }}
        >
          发送指令
        </Button>
      </div>
    </Section>
  );
}

function ApprovalHistory({ approvals }: { approvals: Approval[] }) {
  if (!approvals.length) return null;
  return (
    <Section title="审批记录">
      <ul className="m-0 list-none p-0">
        {approvals.map((approval, index) => {
          const answer = approvalAnswer(approval);
          const who = approvalBy(approval);
          return (
            <li key={index} className="text-muted-foreground grid grid-cols-[58px_120px_1fr] gap-2.5 border-b py-1.5 text-[11.5px] last:border-b-0">
              <time className="text-muted-foreground/80 font-mono text-[11px] tabular-nums">
                {clockTime(approval.decided_at || approval.created_at)}
              </time>
              <span className="truncate font-mono text-[11px]">{approval.stage}</span>
              <span>
                {approvalState(approval)}
                {answer ? ` · ${answer}` : ""}
                {who && approval.status !== "open" ? ` · ${who}` : ""}
              </span>
            </li>
          );
        })}
      </ul>
    </Section>
  );
}

function DeliveryRun({ runs }: { runs?: Run[] }) {
  const run = deliveryRun(runs);
  if (!run) return null;
  const seconds = Math.max(0, Math.round((run.finished_at || 0) - (run.started_at || 0)));
  return (
    <Section title={`交付结果 · ${run.stage}${seconds ? ` · ${seconds}s` : ""}`}>
      <div
        className="bg-muted/20 max-h-[30vh] overflow-auto rounded-md border p-3 text-[13px] leading-relaxed whitespace-pre-wrap break-words"
        dangerouslySetInnerHTML={{ __html: mdInline(String(run.output || "")) }}
      />
    </Section>
  );
}

function RunsList({ runs }: { runs: Run[] }) {
  if (!runs.length) return <p className="text-muted-foreground text-xs">暂无运行记录</p>;
  return (
    <div className="space-y-1.5">
      {runs.slice().reverse().map((run, index) => {
        const seconds = Math.max(0, Math.round((run.finished_at || run.started_at || 0) - (run.started_at || 0)));
        return (
          <div key={index} className="rounded-md border p-2.5 text-xs">
            <div className="flex items-center gap-2">
              <span className={run.ok ? "text-[var(--status-done)]" : "text-[var(--status-failed)]"}>
                {run.ok ? "✓" : "✗"}
              </span>
              <span className="font-mono text-[11px]">{run.stage}</span>
              <span className="text-muted-foreground">{run.agent}</span>
              <span className="text-muted-foreground ml-auto tabular-nums">{seconds}s</span>
            </div>
            {run.output ? (
              <details className="text-muted-foreground mt-1.5 text-[11.5px]">
                <summary className="hover:text-foreground cursor-pointer">输出</summary>
                <pre className="bg-muted/20 mt-1.5 max-h-[46vh] overflow-auto rounded-md border p-2 font-mono text-[11.5px] leading-relaxed break-all whitespace-pre-wrap">
                  {String(run.output).slice(0, 4000)}
                </pre>
              </details>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function ProcessList({ events }: { events: NonNullable<Detail["events"]> }) {
  const rows = processEvents(events);
  if (!rows.length) return null;
  return (
    <Section title="过程">
      <ul className="m-0 list-none p-0">
        {rows.map((event, index) => {
          const kind = String(event.kind || "");
          const tone = /error|fail/.test(kind) ? "text-[var(--status-failed)]" : /done|approved/.test(kind) ? "text-[var(--status-done)]" : "";
          return (
            <li key={index} className="text-muted-foreground grid grid-cols-[58px_120px_1fr] gap-2.5 border-b py-1.5 text-[11.5px] last:border-b-0">
              <time className="text-muted-foreground/80 font-mono text-[11px] tabular-nums">
                {clockTime(event.ts)}
              </time>
              <span className={`truncate font-mono text-[11px] ${tone}`}>{kindLabel(kind)}</span>
              <span>{event.detail || ""}</span>
            </li>
          );
        })}
      </ul>
    </Section>
  );
}