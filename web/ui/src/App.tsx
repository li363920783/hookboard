import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActionIcon,
  Alert,
  Box,
  Button,
  Group,
  Select,
  Text,
  Title,
  Tooltip,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import {
  IconAlertCircle,
  IconLayoutKanban,
  IconPlus,
  IconRefresh,
  IconSettings,
} from "@tabler/icons-react";
import { Board, COLUMNS } from "@/components/Board";
import { ComposerDialog, SettingsDialog } from "@/components/Dialogs";
import { TaskDrawer } from "@/components/TaskDrawer";
import { Unauthorized, decide, fetchBoard, fetchDetail, writeJSON } from "@/lib/api";
import type { BoardData, Detail, Task } from "@/lib/types";

const SOURCE_KEY = "hookboard_source";
const ALL_SOURCES = "*";

/** Hub approval convention: index 0 = 通过, 1 = 打回. */
function decideToast(
  data: { changed?: boolean },
  payload: { index?: number; text?: string },
): string {
  if (data.changed === false) return "这条已经处理过了";
  if (payload.index === 1) return "已打回";
  return "已通过";
}

function isTyping() {
  const el = document.activeElement;
  return !!el && ["TEXTAREA", "INPUT", "SELECT"].includes(el.tagName);
}

export default function App() {
  const [board, setBoard] = useState<BoardData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [slow, setSlow] = useState(false);
  const [source, setSource] = useState(() => localStorage.getItem(SOURCE_KEY) || "");

  const [openId, setOpenId] = useState<number | null>(null);
  const openIdRef = useRef<number | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const [composerOpen, setComposerOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const showToast = useCallback((message: string, isError = false) => {
    notifications.show({
      message,
      color: isError ? "red" : "teal",
      withCloseButton: false,
      autoClose: 4200,
    });
  }, []);

  const handleAuth = useCallback((err: unknown) => {
    if (err instanceof Unauthorized) setSettingsOpen(true);
    showToast(err instanceof Error ? err.message : String(err), true);
  }, [showToast]);

  const refreshing = useRef(false);
  const refresh = useCallback(async () => {
    if (refreshing.current) return;
    refreshing.current = true;
    const timer = window.setTimeout(() => setSlow(true), 180);
    try {
      const data = await fetchBoard();
      setBoard(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      window.clearTimeout(timer);
      setSlow(false);
      setLoading(false);
      refreshing.current = false;
    }
  }, []);

  const refreshDetail = useCallback(async (id: number) => {
    try {
      const next = await fetchDetail(id);
      setDetail(next);
    } catch (err) {
      // Keep the last good detail for transient failures, but an expired token
      // must reopen settings instead of leaving a stale drawer.
      if (err instanceof Unauthorized) handleAuth(err);
    }
  }, [handleAuth]);

  useEffect(() => {
    openIdRef.current = openId;
  }, [openId]);

  useEffect(() => {
    const tick = () => {
      if (document.hidden || isTyping()) return;
      void refresh();
      const id = openIdRef.current;
      if (id) void refreshDetail(id);
    };
    tick();
    const timer = window.setInterval(tick, 3000);
    const onVisible = () => { if (!document.hidden) tick(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [refresh, refreshDetail]);

  const filtered = useMemo(() => {
    const byKey = new Map((board?.columns || []).map((column) => [column.key, column.tasks]));
    return COLUMNS.map((column) => ({
      key: column.key,
      title: column.title,
      tasks: (byKey.get(column.key) || []).filter(
        (task) => !source || String(task.source) === source,
      ),
    }));
  }, [board, source]);

  const projects = board?.projects || [];

  const sources = useMemo(() => {
    const seen = new Set<string>();
    (board?.columns || []).forEach((column) =>
      column.tasks.forEach((task) => { if (task.source) seen.add(String(task.source)); }),
    );
    if (source) seen.add(source);
    return Array.from(seen).sort();
  }, [board, source]);

  const openDrawer = useCallback((task: Task) => {
    setOpenId(task.id);
    setDetail(null);
    setDetailLoading(true);
    fetchDetail(task.id)
      .then((next) => setDetail(next))
      .catch((err) => {
        showToast(err instanceof Error ? err.message : String(err), true);
        setOpenId(null);
      })
      .finally(() => setDetailLoading(false));
  }, [showToast]);

  const closeDrawer = useCallback(() => {
    setOpenId(null);
    setDetail(null);
  }, []);

  const handleDecide = useCallback(
    async (token: string, payload: { index?: number; text?: string }) => {
      try {
        const data = await decide(token, payload);
        showToast(decideToast(data, payload));
        void refresh();
        const id = openIdRef.current;
        if (id) void refreshDetail(id);
        return true;
      } catch (err) {
        handleAuth(err);
        return false;
      }
    },
    [handleAuth, refresh, refreshDetail, showToast],
  );

  const handleCreate = useCallback(
    async (title: string, project: string) => {
      const payload: Record<string, string> = { title, kind: "instruction", source: "web" };
      if (project) payload.project = project;
      try {
        const data = await writeJSON<{ ok: boolean; task_id: number }>("/api/tasks", payload);
        showToast(`任务 #${data.task_id} 已创建`);
        void refresh();
        return true;
      } catch (err) {
        handleAuth(err);
        return false;
      }
    },
    [handleAuth, refresh, showToast],
  );

  const handleMessage = useCallback(
    async (text: string) => {
      const id = openIdRef.current;
      if (!id) return false;
      try {
        const result = await writeJSON<{ ok: boolean }>(`/api/tasks/${id}/message`, { text });
        showToast(result.ok ? "指令已送达会话" : "会话返回：失败", !result.ok);
        void refreshDetail(id);
        return true;
      } catch (err) {
        handleAuth(err);
        return false;
      }
    },
    [handleAuth, refreshDetail, showToast],
  );

  const withTaskAction = useCallback(
    async (suffix: string, body: unknown, done: (data: { ok: boolean; task?: { id: number } }) => string) => {
      const id = openIdRef.current;
      if (!id) return;
      try {
        const data = await writeJSON<{ ok: boolean; task?: { id: number } }>(`/api/tasks/${id}/${suffix}`, body);
        showToast(done(data));
        void refreshDetail(id);
        void refresh();
      } catch (err) {
        handleAuth(err);
      }
    },
    [handleAuth, refresh, refreshDetail, showToast],
  );

  const handleCancel = useCallback(() => {
    if (!window.confirm("取消这个任务？正在进行的 agent 不会被强杀，但任务不再推进。")) return;
    void withTaskAction("cancel", {}, () => "任务已取消");
  }, [withTaskAction]);

  const handleRetry = useCallback(() => {
    void withTaskAction("retry", {}, (data) => `已重新入队，任务 #${data.task?.id ?? ""} 会从当前阶段继续`);
  }, [withTaskAction]);

  const handleWake = useCallback(() => {
    void withTaskAction("wake", {}, () => "已唤醒，等待 worker 处理");
  }, [withTaskAction]);

  const total = filtered.reduce((sum, column) => sum + column.tasks.length, 0);
  const online = !error;
  const showSkeleton = loading && !board;

  return (
    <Box h="100%" style={{ display: "flex", flexDirection: "column" }}>
      <Box
        aria-hidden
        style={{
          position: "fixed",
          inset: "0 0 auto 0",
          height: 2,
          overflow: "hidden",
          zIndex: 60,
          opacity: slow ? 1 : 0,
          transition: "opacity .15s",
        }}
      >
        <Box
          className={slow ? "hb-progress" : undefined}
          style={{ width: "25%", height: "100%", background: "var(--mantine-color-indigo-5)" }}
        />
      </Box>

      <Box
        component="header"
        px="md"
        py={10}
        style={{
          position: "sticky",
          top: 0,
          zIndex: 40,
          flexShrink: 0,
          background: "var(--mantine-color-dark-8)",
          borderBottom: "1px solid var(--mantine-color-dark-5)",
        }}
      >
        <Group gap="md" wrap="wrap">
          <Group gap={8} wrap="nowrap">
            <Box
              style={{
                width: 26,
                height: 26,
                borderRadius: 8,
                display: "grid",
                placeItems: "center",
                background: "linear-gradient(135deg, #4f7cff, #8b5cf6)",
                color: "#fff",
              }}
            >
              <IconLayoutKanban size={15} stroke={2} />
            </Box>
            <Text size="sm" fw={700} lh={1}>Hookboard</Text>
            <Tooltip label={online ? "轮询 /api/board" : "无法连接后端"} withArrow>
              <Box
                style={{
                  width: 7,
                  height: 7,
                  borderRadius: 999,
                  background: online ? "var(--mantine-color-teal-5)" : "var(--mantine-color-red-6)",
                }}
              />
            </Tooltip>
          </Group>

          <Group component="nav" gap={2} wrap="wrap" aria-label="状态统计">
            {COLUMNS.map((column, index) => (
              <Group
                key={column.key}
                gap={6}
                px={8}
                py={4}
                wrap="nowrap"
                style={{ borderRadius: 8 }}
              >
                <Box
                  style={{
                    width: 7,
                    height: 7,
                    borderRadius: 999,
                    background: `var(--mantine-color-${column.tone}-5)`,
                  }}
                />
                <Text size="xs" fw={700} ff="monospace" lh={1}>
                  {filtered[index]?.tasks.length ?? 0}
                </Text>
                <Text size="xs" c="dimmed" lh={1}>{column.title}</Text>
              </Group>
            ))}
          </Group>

          <Group gap="xs" ml="auto" wrap="nowrap">
            <Select
              size="xs"
              w={132}
              id="source-filter"
              name="source"
              aria-label="按来源筛选"
              data={[
                { value: ALL_SOURCES, label: "全部来源" },
                ...sources.map((name) => ({ value: name, label: name })),
              ]}
              value={source || ALL_SOURCES}
              onChange={(next) => {
                const value = !next || next === ALL_SOURCES ? "" : next;
                setSource(value);
                localStorage.setItem(SOURCE_KEY, value);
              }}
              allowDeselect={false}
              comboboxProps={{ withinPortal: true }}
            />
            <Button size="xs" leftSection={<IconPlus size={14} />} onClick={() => setComposerOpen(true)}>
              新任务
            </Button>
            <Tooltip label="API Token 设置" withArrow>
              <ActionIcon
                size="lg"
                variant="default"
                aria-label="API Token 设置"
                onClick={() => setSettingsOpen(true)}
              >
                <IconSettings size={16} />
              </ActionIcon>
            </Tooltip>
          </Group>
        </Group>
      </Box>

      {error ? (
        <Alert
          color="red"
          variant="light"
          radius={0}
          py={6}
          icon={<IconAlertCircle size={16} />}
          style={{ flexShrink: 0 }}
        >
          <Group justify="space-between" gap="xs" wrap="nowrap">
            <Text size="xs">无法连接后端：{error}</Text>
            <Button
              size="compact-xs"
              variant="subtle"
              color="red"
              leftSection={<IconRefresh size={12} />}
              onClick={() => void refresh()}
            >
              重试
            </Button>
          </Group>
        </Alert>
      ) : null}

      <Group gap="sm" align="baseline" px="md" pt="md" style={{ flexShrink: 0 }} wrap="wrap">
        <Title order={4} fz={15} lh={1.2}>任务看板</Title>
        <Text size="xs" c="dimmed">
          {showSkeleton
            ? "正在加载任务…"
            : `${total} 个任务 · 按状态分列 · 每 3 秒自动刷新，点击卡片查看详情`}
        </Text>
      </Group>

      <Board
        columns={showSkeleton ? [] : filtered}
        loading={showSkeleton}
        onOpen={openDrawer}
        onDecide={handleDecide}
      />

      <TaskDrawer
        open={openId !== null}
        detail={detail}
        loading={detailLoading}
        onClose={closeDrawer}
        onDecide={handleDecide}
        onMessage={handleMessage}
        onCancel={handleCancel}
        onRetry={handleRetry}
        onWake={handleWake}
        onReload={() => {
          const id = openIdRef.current;
          if (!id) return;
          setDetailLoading(true);
          void refreshDetail(id).finally(() => setDetailLoading(false));
        }}
      />

      <ComposerDialog
        open={composerOpen}
        projects={projects}
        onOpenChange={setComposerOpen}
        onCreate={handleCreate}
      />

      <SettingsDialog open={settingsOpen} onOpenChange={setSettingsOpen} onSaved={(message) => showToast(message)} />
    </Box>
  );
}