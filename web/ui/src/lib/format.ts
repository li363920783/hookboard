import type { Approval, Run, TaskEvent } from "./types";

export function elapsed(ts?: number): string {
  const n = Number(ts);
  if (!Number.isFinite(n) || !n) return "—";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - n));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

export function clock(ts?: number): string {
  const n = Number(ts);
  if (!Number.isFinite(n) || !n) return "—";
  return new Date(n * 1000).toLocaleString("zh-CN", { hour12: false });
}

export function clockTime(ts?: number): string {
  const n = Number(ts);
  if (!Number.isFinite(n) || !n) return "—";
  return new Date(n * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}

export function num(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

const KIND_LABEL: Record<string, string> = {
  created: "创建",
  approval_open: "发起审批",
  approved: "审批通过",
  rejected: "审批打回",
  done: "任务完成",
  failed: "失败",
  merged: "并入原任务",
  message: "发送指令",
  message_error: "指令发送失败",
  requeue: "重新入队",
  retry: "重试",
  cancelled: "取消",
  notify_error: "通知失败",
  session_step: "继续会话",
};

export function kindLabel(kind: string): string {
  return KIND_LABEL[kind] || kind;
}

/** Ticket bodies and agent reports are light markdown; render bold and code spans. */
export function mdInline(text: string): string {
  // NUL is the placeholder delimiter below, so real ones must never survive.
  const escaped = text
    .replace(/\u0000/g, "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  // Code spans are lifted out first: whatever sits between backticks must not
  // be bolded, and long commands/URLs get their own wrapping style.
  const codes: string[] = [];
  const held = escaped.replace(/`([^`\n]{1,300})`/g, (_, inner: string) => {
    codes.push(inner);
    return `\u0000${codes.length - 1}\u0000`;
  });
  return held
    .replace(/\*\*([^*\n]{1,80})\*\*/g, "<strong>$1</strong>")
    .replace(/\u0000(\d+)\u0000/g, (_, index: string) => {
      const code = codes[Number(index)];
      return code === undefined ? "" : `<code>${code}</code>`;
    });
}

export function approvalState(ap: Approval): string {
  if (ap.status === "open") return "待处理";
  if (ap.status === "superseded") return "已作废";
  return Number(ap.answer_index) === 1 ? "打回" : "通过";
}

export function approvalAnswer(ap: Approval): string {
  const answer = String(ap.answer_text || "").trim();
  return answer === "通过" || answer === "打回" ? "" : answer;
}

export function approvalBy(ap: Approval): string {
  const who = String(ap.by || "");
  return who.indexOf("feishu:") === 0 ? `飞书 …${who.slice(-6)}` : who;
}

/** What the agent actually delivered: last successful report-like work run. */
export function deliveryRun(runs?: Run[]): Run | null {
  let best: Run | null = null;
  let bestLen = 0;
  for (const run of runs || []) {
    const out = String(run.output || "");
    if (!run.ok || out.trim().length < 40) continue;
    if (/^\s*\{/.test(out)) continue;
    if (!["work", "release", "finish"].includes(run.stage)) continue;
    if (out.length >= bestLen) {
      best = run;
      bestLen = out.length;
    }
  }
  return best;
}

export function processEvents(events?: TaskEvent[]): TaskEvent[] {
  return (events || []).filter((event) => event.kind !== "stage").slice().reverse();
}