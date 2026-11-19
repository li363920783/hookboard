import { Badge, Box, Card, Group, Skeleton, Stack, Text, UnstyledButton } from "@mantine/core";
import { ApprovalForm } from "@/components/ApprovalForm";
import { elapsed } from "@/lib/format";
import type { Column, Task } from "@/lib/types";

export const COLUMNS: { key: string; title: string; tone: string; empty: string }[] = [
  { key: "pending", title: "待处理", tone: "gray", empty: "暂无任务" },
  { key: "running", title: "进行中", tone: "blue", empty: "没有正在执行的任务" },
  { key: "waiting", title: "等你审批", tone: "yellow", empty: "没有待审批项" },
  { key: "done", title: "已完成", tone: "teal", empty: "还没有已完成任务" },
  { key: "failed", title: "失败", tone: "red", empty: "没有失败任务" },
];

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
    <Box className="hb-board">
      {COLUMNS.map((column) => {
        const tasks = byKey.get(column.key) || [];
        return (
          <Card
            key={column.key}
            className="hb-col hb-lane"
            padding={8}
            radius="xl"
            withBorder
            style={{
              display: "flex",
              flexDirection: "column",
              minHeight: 0,
            }}
          >
            <Group gap={8} px={4} mb={8} wrap="nowrap">
              <Box
                className={column.key === "running" ? "hb-pulse" : undefined}
                style={{
                  width: 8,
                  height: 8,
                  borderRadius: 999,
                  background: `var(--mantine-color-${column.tone}-5)`,
                  flexShrink: 0,
                }}
              />
              <Text size="sm" fw={600} lh={1.2}>{column.title}</Text>
              <Badge size="xs" variant="light" color={column.tone} radius="xl" ml="auto">
                {tasks.length}
              </Badge>
            </Group>

            <Box
              className="hb-col-body hb-scroll"
              style={{ flex: 1, minHeight: 0, overflowY: "auto" }}
            >
              <Stack gap={8} pr={4}>
                {loading && columns.length === 0 ? (
                  <>
                    <Skeleton height={84} radius="md" />
                    <Skeleton height={68} radius="md" />
                  </>
                ) : tasks.length === 0 ? (
                  <Card
                    padding="lg"
                    radius="lg"
                    withBorder
                    style={{
                      borderStyle: "dashed",
                      background: "transparent",
                      borderColor: "var(--mantine-color-dark-5)",
                    }}
                  >
                    <Text size="xs" c="dimmed" ta="center">{column.empty}</Text>
                  </Card>
                ) : (
                  tasks.map((task) => (
                    <TaskCard key={task.id} task={task} onOpen={onOpen} onDecide={onDecide} />
                  ))
                )}
              </Stack>
            </Box>
          </Card>
        );
      })}
    </Box>
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
  const tone = COLUMNS.find((column) => column.key === task.status)?.tone || "gray";
  return (
    <Card
      className="hb-task hb-card"
      data-tone={tone}
      data-status={task.status}
      padding="sm"
      radius="lg"
      withBorder
      onClick={() => onOpen(task)}
      style={{ cursor: "pointer" }}
    >
      <Group justify="space-between" gap={8} mb={6} wrap="nowrap">
        <Text size="xs" c="dimmed" fw={600} tt="uppercase" style={{ letterSpacing: ".05em" }} truncate>
          {task.source}
        </Text>
        <Text size="xs" c="dimmed" ff="monospace" truncate style={{ maxWidth: "62%" }}>
          {task.external_id}
        </Text>
      </Group>

      <UnstyledButton
        onClick={(event) => { event.stopPropagation(); onOpen(task); }}
        style={{ display: "block", width: "100%", textAlign: "left" }}
      >
        <Text size="sm" fw={600} lh={1.45} lineClamp={3}>{task.title}</Text>
      </UnstyledButton>

      <Group gap={6} mt={8} wrap="wrap">
        <Badge size="sm" variant="light" color={tone} radius="xl">{task.stage}</Badge>
        <Text size="xs" c="dimmed">{elapsed(task.created_at)}</Text>
        {task.session && ACTIVE.includes(task.status) ? (
          <Badge size="sm" variant="light" color="indigo" radius="xl" ml="auto">已派单</Badge>
        ) : null}
      </Group>

      {task.note ? (
        <Text size="xs" c="dimmed" mt={8} lineClamp={2} lh={1.5}>{task.note}</Text>
      ) : null}

      {task.approval ? (
        <Card
          mt={8}
          padding="xs"
          radius="lg"
          withBorder
          onClick={(event) => event.stopPropagation()}
          style={{
            cursor: "default",
            background: "color-mix(in srgb, var(--mantine-color-yellow-6) 8%, transparent)",
            borderColor: "color-mix(in srgb, var(--mantine-color-yellow-6) 40%, transparent)",
          }}
        >
          <ApprovalForm key={task.approval.token} approval={task.approval} onDecide={onDecide} />
        </Card>
      ) : null}
    </Card>
  );
}