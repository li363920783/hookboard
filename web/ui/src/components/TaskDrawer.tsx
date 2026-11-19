import { useState } from "react";
import {
  Accordion,
  Badge,
  Box,
  Button,
  Code,
  Divider,
  Drawer,
  Group,
  Paper,
  ScrollArea,
  Skeleton,
  Stack,
  Table,
  Text,
  Textarea,
  Timeline,
  Title,
} from "@mantine/core";
import { IconArrowBackUp, IconPlayerPlay, IconRefresh, IconTrash } from "@tabler/icons-react";
import { ApprovalForm } from "@/components/ApprovalForm";
import {
  approvalAnswer,
  approvalBy,
  approvalState,
  clock,
  clockTime,
  deliveryRun,
  elapsed,
  kindLabel,
  mdInline,
  num,
  processEvents,
} from "@/lib/format";
import type { Approval, Detail, Run } from "@/lib/types";

// One long line (JSON output, a build log) must not widen the drawer.
const WRAP = { whiteSpace: "pre-wrap", overflowWrap: "anywhere" } as const;

const STATUS_TONE: Record<string, string> = {
  pending: "gray",
  running: "blue",
  waiting: "yellow",
  done: "teal",
  failed: "red",
  cancelled: "red",
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
    <Drawer
      opened={open}
      onClose={onClose}
      position="right"
      size={560}
      padding="md"
      scrollAreaComponent={ScrollArea.Autosize}
      overlayProps={{ backgroundOpacity: 0.55, blur: 2 }}
      styles={{
        content: {
          background: "rgba(15,16,24,.82)",
          backdropFilter: "blur(26px) saturate(150%)",
          WebkitBackdropFilter: "blur(26px) saturate(150%)",
          borderLeft: "1px solid rgba(255,255,255,.08)",
        },
        // Opaque enough that the body does not read through the title while
        // scrolling, but still the same frosted surface.
        header: {
          background: "rgba(15,16,24,.94)",
          backdropFilter: "blur(18px)",
          WebkitBackdropFilter: "blur(18px)",
          borderBottom: "1px solid rgba(255,255,255,.07)",
        },
        body: { paddingBottom: 28, overflowX: "hidden" },
      }}
      title={
        task ? (
          <Box style={{ minWidth: 0 }}>
            <Group gap={6} wrap="nowrap">
              <Badge size="sm" variant="light" color={STATUS_TONE[task.status] || "gray"} radius="xl">
                {task.status}
              </Badge>
              <Badge size="sm" variant="light" color="gray" radius="xl">{task.stage}</Badge>
              <Badge size="sm" variant="light" color="indigo" radius="xl">{task.source}</Badge>
              <Text size="xs" c="dimmed" ff="monospace" truncate>{task.external_id}</Text>
            </Group>
            <Title order={5} mt={6} fz={15} lh={1.4} lineClamp={2} style={{ maxWidth: 430 }}>
              {task.title}
            </Title>
          </Box>
        ) : (
          <Group gap={6}>
            <Skeleton height={20} width={54} radius="xl" />
            <Skeleton height={20} width={54} radius="xl" />
            <Skeleton height={20} width={54} radius="xl" />
          </Group>
        )
      }
    >
      {!detail ? (
        loading ? (
          <Stack gap="sm">
            <Skeleton height={160} radius="lg" />
            <Skeleton height={96} radius="lg" />
            <Skeleton height={96} radius="lg" />
          </Stack>
        ) : (
          <Group gap="xs">
            <Text size="xs" c="dimmed">加载失败，请重试。</Text>
            <Button size="compact-xs" variant="light" leftSection={<IconRefresh size={12} />} onClick={onReload}>
              重试
            </Button>
          </Group>
        )
      ) : (
        <Stack gap="lg">
          <Section title="概览">
            <Table withRowBorders={false} verticalSpacing={5} fz="xs" horizontalSpacing={0} style={{ tableLayout: "fixed", width: "100%" }}>
              <Table.Tbody>
                <Fact label="状态" value={`${task!.status} / ${task!.stage}`} />
                <Fact label="项目" value={task!.project || "—"} />
                <Fact label="派单目标" value={task!.workspace ? `${task!.workspace}${task!.branch ? ` @ ${task!.branch}` : ""}` : "—"} mono />
                <Fact label="会话" value={task!.session || "—"} mono />
                <Fact label="来源 / 类型" value={`${task!.source} · ${task!.kind || "—"}`} />
                <Fact label="条目" value={task!.external_id} mono />
                <Fact label="创建" value={`${clock(task!.created_at)}（${elapsed(task!.created_at)}）`} />
                <Fact label="更新" value={clock(task!.updated_at)} />
                <Fact label="尝试" value={String(num(task!.attempts))} />
              </Table.Tbody>
            </Table>
            {task!.note ? <Text size="xs" c="dimmed" mt="xs" lh={1.5}>{task!.note}</Text> : null}
          </Section>

          {String(task!.context || "").trim() ? (
            <Section title="需求详情">
              <Paper p="sm" radius="lg" withBorder style={{ background: "rgba(255,255,255,.04)" }}>
                <Box
                  className="hb-prose"
                  style={{ fontSize: 13, lineHeight: 1.6, whiteSpace: "pre-wrap" }}
                  dangerouslySetInnerHTML={{ __html: mdInline(String(task!.context)) }}
                />
              </Paper>
            </Section>
          ) : null}

          <DeliveryRun runs={detail.runs} />

          {detail.approvals?.filter((approval) => approval.status === "open").map((approval) => (
            <Section key={approval.token} title={`等你审批 · ${approval.stage || ""}`}>
              <Paper p="sm" radius="lg" withBorder style={{ background: "rgba(255,255,255,.04)" }}>
                <ApprovalForm approval={approval} onDecide={onDecide} />
              </Paper>
            </Section>
          ))}

          {task!.status === "running" && task!.session ? <MessageBox onMessage={onMessage} /> : null}

          <ApprovalHistory approvals={detail.approvals || []} />

          <Section title="运行记录">
            <RunsTable runs={detail.runs || []} />
          </Section>

          <ProcessList events={detail.events || []} />

          <Section title="原始数据">
            <Accordion variant="separated" radius="lg" chevronPosition="right">
              <Accordion.Item value="payload">
                <Accordion.Control fz="xs">报文 payload</Accordion.Control>
                <Accordion.Panel>
                  <Code
                    block
                    style={{
                      maxHeight: "40vh",
                      overflow: "auto",
                      fontSize: 11,
                      ...WRAP,
                    }}
                  >
                    {JSON.stringify(task!.payload || {}, null, 2)}
                  </Code>
                </Accordion.Panel>
              </Accordion.Item>
              <Accordion.Item value="events">
                <Accordion.Control fz="xs">原始事件 {(detail.events || []).length} 条</Accordion.Control>
                <Accordion.Panel>
                  <Code
                    block
                    style={{
                      maxHeight: "40vh",
                      overflow: "auto",
                      fontSize: 11,
                      ...WRAP,
                    }}
                  >
                    {(detail.events || []).slice(-300).map((event) => `${clock(event.ts)}  ${event.kind}  ${event.detail || ""}`).join("\n")}
                  </Code>
                </Accordion.Panel>
              </Accordion.Item>
            </Accordion>
          </Section>

          <Divider />
          <Group justify="flex-end" gap="xs">
            {task!.status === "failed" ? (
              <Button size="xs" leftSection={<IconRefresh size={14} />} onClick={onRetry}>重试</Button>
            ) : null}
            {task!.status === "pending" ? (
              <Button size="xs" variant="light" leftSection={<IconPlayerPlay size={14} />} onClick={onWake}>唤醒</Button>
            ) : null}
            {["pending", "running", "waiting"].includes(task!.status) ? (
              <Button
                size="xs"
                color="red"
                variant="light"
                leftSection={<IconTrash size={14} />}
                onClick={onCancel}
              >
                取消任务
              </Button>
            ) : null}
          </Group>
        </Stack>
      )}
    </Drawer>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Box>
      <Text size="xs" fw={700} c="dimmed" tt="uppercase" mb={8} style={{ letterSpacing: ".06em" }}>
        {title}
      </Text>
      {children}
    </Box>
  );
}

function Fact({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <Table.Tr>
      <Table.Td w={88} style={{ color: "var(--mantine-color-dark-2)", verticalAlign: "top" }}>{label}</Table.Td>
      <Table.Td style={{ overflowWrap: "anywhere" }}>
        <Text size="xs" ff={mono ? "monospace" : undefined} lh={1.5}>{value}</Text>
      </Table.Td>
    </Table.Tr>
  );
}

function MessageBox({ onMessage }: { onMessage: (text: string) => Promise<boolean> }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <Section title="给运行中的会话发指令">
      <Textarea
        size="xs"
        rows={2}
        name="message"
        placeholder="补充要求、纠正方向、让它先停下来（忙时会作为下一轮消息）"
        value={text}
        onChange={(event) => setText(event.currentTarget.value)}
      />
      <Group justify="flex-end" mt="xs">
        <Button
          size="compact-sm"
          variant="light"
          leftSection={<IconArrowBackUp size={14} />}
          disabled={busy || !text.trim()}
          onClick={async () => {
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
      </Group>
    </Section>
  );
}

function ApprovalHistory({ approvals }: { approvals: Approval[] }) {
  if (!approvals.length) return null;
  return (
    <Section title="审批记录">
      <Table striped highlightOnHover withRowBorders={false} verticalSpacing={5} fz="xs">
        <Table.Thead>
          <Table.Tr>
            <Table.Th w={72}>时间</Table.Th>
            <Table.Th w={96}>阶段</Table.Th>
            <Table.Th>结果</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {approvals.map((approval, index) => {
            const answer = approvalAnswer(approval);
            const who = approvalBy(approval);
            return (
              <Table.Tr key={index}>
                <Table.Td>
                  <Text size="xs" c="dimmed" ff="monospace">{clockTime(approval.decided_at || approval.created_at)}</Text>
                </Table.Td>
                <Table.Td><Text size="xs" ff="monospace" truncate>{approval.stage}</Text></Table.Td>
                <Table.Td>
                  <Text size="xs">
                    {approvalState(approval)}
                    {answer ? ` · ${answer}` : ""}
                    {who && approval.status !== "open" ? ` · ${who}` : ""}
                  </Text>
                </Table.Td>
              </Table.Tr>
            );
          })}
        </Table.Tbody>
      </Table>
    </Section>
  );
}

function DeliveryRun({ runs }: { runs?: Run[] }) {
  const run = deliveryRun(runs);
  if (!run) return null;
  const seconds = Math.max(0, Math.round((run.finished_at || 0) - (run.started_at || 0)));
  return (
    <Section title={`交付结果 · ${run.stage}${seconds ? ` · ${seconds}s` : ""}`}>
      <Paper p="sm" radius="lg" withBorder style={{ background: "rgba(255,255,255,.04)" }}>
        <Box
          className="hb-prose"
          style={{ fontSize: 13, lineHeight: 1.6, whiteSpace: "pre-wrap" }}
          dangerouslySetInnerHTML={{ __html: mdInline(String(run.output || "")) }}
        />
      </Paper>
    </Section>
  );
}

function RunsTable({ runs }: { runs: Run[] }) {
  if (!runs.length) return <Text size="xs" c="dimmed">暂无运行记录</Text>;
  return (
    <Table
      highlightOnHover
      withRowBorders={false}
      verticalSpacing={5}
      fz="xs"
      style={{ tableLayout: "fixed", width: "100%" }}
    >
      <Table.Thead>
        <Table.Tr>
          <Table.Th w={80}>阶段</Table.Th>
          <Table.Th w={80}>执行器</Table.Th>
          <Table.Th w={64}>结果</Table.Th>
          <Table.Th w={60}>耗时</Table.Th>
          <Table.Th>输出</Table.Th>
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {runs.slice().reverse().map((run, index) => {
          const seconds = Math.max(0, Math.round((run.finished_at || run.started_at || 0) - (run.started_at || 0)));
          return (
            <Table.Tr key={`${run.stage}-${run.started_at ?? index}-${index}`}>
              <Table.Td><Text size="xs" ff="monospace">{run.stage}</Text></Table.Td>
              <Table.Td><Text size="xs" c="dimmed">{run.agent}</Text></Table.Td>
              <Table.Td>
                <Badge size="sm" variant="light" color={run.ok ? "teal" : "red"} radius="xl">
                  {run.ok ? "成功" : "失败"}
                </Badge>
              </Table.Td>
              <Table.Td><Text size="xs" c="dimmed" ff="monospace">{seconds}s</Text></Table.Td>
              <Table.Td>
                <Box>
                  <Text size="xs" c="dimmed" lineClamp={2} lh={1.45}>{String(run.output || "—").slice(0, 160)}</Text>
                  {String(run.output || "").length > 160 ? (
                    <details style={{ marginTop: 4 }}>
                      <summary style={{ cursor: "pointer", fontSize: 11, color: "var(--mantine-color-dimmed)" }}>
                        展开全部
                      </summary>
                      <Code
                        block
                        style={{
                          maxHeight: "32vh",
                          overflow: "auto",
                          fontSize: 11,
                          marginTop: 6,
                          ...WRAP,
                        }}
                      >
                        {String(run.output)}
                      </Code>
                    </details>
                  ) : null}
                </Box>
              </Table.Td>
            </Table.Tr>
          );
        })}
      </Table.Tbody>
    </Table>
  );
}

function ProcessList({ events }: { events: NonNullable<Detail["events"]> }) {
  const rows = processEvents(events);
  if (!rows.length) return null;
  return (
    <Section title="过程">
      <Timeline bulletSize={12} lineWidth={1} color="gray" active={rows.length}>
        {rows.map((event, index) => {
          const kind = String(event.kind || "");
          const tone = /error|fail/.test(kind) ? "red" : /done|approved/.test(kind) ? "teal" : "gray";
          return (
            <Timeline.Item
              key={index}
              bullet={
                <Box
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: 999,
                    background: `var(--mantine-color-${tone}-5)`,
                  }}
                />
              }
              title={
                <Group gap={8} wrap="nowrap">
                  <Text size="xs" fw={600} ff="monospace">{kindLabel(kind)}</Text>
                  <Text size="xs" c="dimmed" ff="monospace">{clockTime(event.ts)}</Text>
                </Group>
              }
            >
              {event.detail ? <Text size="xs" c="dimmed" lh={1.45}>{event.detail}</Text> : null}
            </Timeline.Item>
          );
        })}
      </Timeline>
    </Section>
  );
}