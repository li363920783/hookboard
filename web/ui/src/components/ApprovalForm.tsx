import { useState } from "react";
import { Button, Group, Stack, Text, Textarea } from "@mantine/core";
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

  if (done) {
    return (
      <Text size="xs" c="yellow.5">已提交，正在刷新…</Text>
    );
  }

  return (
    <Stack gap={8}>
      <Text size="xs" c="yellow.5" lh={1.5}>{approval.question}</Text>
      <Group gap={6}>
        {(approval.options || []).map((label, index) => (
          <Button
            key={index}
            type="button"
            size="compact-sm"
            radius="sm"
            variant={index === approval.recommend ? "filled" : "default"}
            disabled={busy}
            onClick={() => run({ index })}
          >
            {label}
          </Button>
        ))}
      </Group>
      <Textarea
        size="xs"
        rows={2}
        name="approval-comment"
        placeholder="补充意见（提交视为通过）"
        value={text}
        onChange={(event) => setText(event.currentTarget.value)}
      />
      <Button
        type="button"
        size="compact-sm"
        variant="light"
        fullWidth
        disabled={busy || !text.trim()}
        onClick={() => run({ text: text.trim() })}
      >
        提交
      </Button>
    </Stack>
  );
}