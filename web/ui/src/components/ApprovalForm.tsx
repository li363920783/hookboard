import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { Approval } from "@/lib/types";

/** Decision index convention comes from the hub: 0 = 通过, 1 = 打回. */
export function ApprovalForm({
  approval,
  onDecide,
}: {
  approval: Approval;
  onDecide: (token: string, payload: { index?: number; text?: string }) => Promise<boolean>;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  async function run(payload: { index?: number; text?: string }) {
    setBusy(true);
    try {
      const ok = await onDecide(approval.token, payload);
      if (ok) setDone(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <p className="m-0 mb-2 text-xs" style={{ color: "var(--status-waiting)" }}>
        {done ? "已提交，正在刷新…" : approval.question}
      </p>
      {done ? null : (
        <>
          <div className="flex flex-wrap gap-1.5">
            {(approval.options || []).map((label, index) => (
              <Button
                key={label}
                size="sm"
                variant={index === approval.recommend ? "default" : "outline"}
                disabled={busy}
                onClick={() => run({ index })}
              >
                {label}
              </Button>
            ))}
          </div>
          <Textarea
            rows={2}
            name="approval-comment"
            className="mt-2 min-h-14 text-xs"
            placeholder="补充意见（提交视为通过）"
            value={text}
            onChange={(event) => setText(event.target.value)}
          />
          <Button
            size="sm"
            variant="secondary"
            className="mt-1.5 w-full"
            disabled={busy || !text.trim()}
            onClick={() => run({ text: text.trim() })}
          >
            提交
          </Button>
        </>
      )}
    </>
  );
}