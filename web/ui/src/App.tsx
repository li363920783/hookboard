import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertCircle, Boxes, Plus, RefreshCw, Settings } from "lucide-react";
import { Board, COLUMNS } from "@/components/Board";
import { ComposerDialog, SettingsDialog } from "@/components/Dialogs";
import { TaskDrawer } from "@/components/TaskDrawer";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { Unauthorized, decide, fetchBoard, fetchDetail, writeJSON } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { BoardData, Detail, Task } from "@/lib/types";

const SOURCE_KEY = "hookboard_source";

/** Hub approval convention: index 0 = 通过, index 1 = 打回. */
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

  const [toast, setToast] = useState<{ id: number; message: string; error: boolean } | null>(null);
  const toastTimer = useRef<number | null>(null);

  const showToast = useCallback((message: string, isError = false) => {
    const id = Date.now();
    setToast({ id, message, error: isError });
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 4200);
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
    <div className="flex h-full flex-col">
      <div
        aria-hidden
        className="pointer-events-none fixed inset-x-0 top-0 z-[60] h-0.5 overflow-hidden"
        style={{ opacity: slow ? 1 : 0, transition: "opacity .15s" }}
      >
        <div
          className="bg-primary h-full w-1/3"
          style={{ animation: slow ? "hb-progress 1.1s ease-in-out infinite" : undefined }}
        />
      </div>

      <header className="bg-background sticky top-0 z-30 flex min-h-13 shrink-0 flex-wrap items-center gap-x-4 gap-y-2 border-b px-4 py-2">
        <div className="flex items-center gap-2">
          <span className="bg-card grid size-[22px] place-items-center rounded-md border">
            <Boxes className="text-muted-foreground size-3.5" />
          </span>
          <span className="text-sm font-semibold">Hookboard</span>
          <span
            title={online ? "轮询 /api/board" : "无法连接后端"}
            className={cn(
              "size-1.5 rounded-full",
              online ? "bg-[var(--status-done)]" : "bg-[var(--status-failed)]",
            )}
          />
        </div>

        <nav className="flex flex-wrap items-center gap-0.5 sm:gap-1" aria-label="状态统计">
          {COLUMNS.map((column, index) => (
            <span
              key={column.key}
              className="text-muted-foreground hover:bg-accent flex items-center gap-1.5 rounded-md px-1.5 py-1 text-xs sm:px-2"
            >
              <span className={cn("size-1.5 rounded-full", column.dot)} />
              <b className="text-foreground font-mono text-xs font-semibold tabular-nums">
                {filtered[index]?.tasks.length ?? 0}
              </b>
              {column.title}
            </span>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-2 border-l pl-4">
          <Select
            id="source-filter"
            name="source"
            value={source}
            aria-label="按来源筛选"
            onChange={(event) => {
              const next = event.target.value;
              setSource(next);
              localStorage.setItem(SOURCE_KEY, next);
            }}
            className={cn("w-[100px] sm:w-[112px]", source && "[&_select]:border-ring/60 [&_select]:text-foreground")}
          >
            <option value="">全部来源</option>
            {sources.map((name) => (
              <option key={name} value={name}>{name}</option>
            ))}
          </Select>
          <Button onClick={() => setComposerOpen(true)}>
            <Plus />
            新任务
          </Button>
          <Button variant="ghost" size="icon" aria-label="API Token 设置" title="API Token 设置" onClick={() => setSettingsOpen(true)}>
            <Settings />
          </Button>
        </div>
      </header>

      {error ? (
        <div
          role="alert"
          className="bg-destructive/10 border-destructive/30 text-destructive flex items-center gap-2 border-b px-4 py-1.5 text-xs"
        >
          <AlertCircle className="size-3.5 shrink-0" />
          无法连接后端：{error}
          <button
            type="button"
            onClick={() => void refresh()}
            className="hover:bg-destructive/15 ml-auto inline-flex items-center gap-1 rounded px-1.5 py-0.5"
          >
            <RefreshCw className="size-3" />
            重试
          </button>
        </div>
      ) : null}

      <div className="flex shrink-0 flex-wrap items-baseline gap-x-3 gap-y-1 px-4 pt-4">
        <h1 className="m-0 text-[15px] font-semibold">任务看板</h1>
        <p className="text-muted-foreground m-0 text-xs">
          {showSkeleton
            ? "正在加载任务…"
            : `${total} 个任务 · 按状态分列 · 每 3 秒自动刷新，点击卡片查看详情`}
        </p>
      </div>

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

      {toast ? (
        <div
          key={toast.id}
          role="status"
          aria-live="polite"
          className={cn(
            "bg-popover text-popover-foreground fixed bottom-6 left-1/2 z-[80] max-w-[90vw] -translate-x-1/2 rounded-lg border px-4 py-2 text-sm shadow-lg",
            toast.error && "bg-destructive/10 border-destructive/40 text-destructive",
          )}
          style={{ animation: "hb-toast-in .16s ease" }}
        >
          {toast.message}
        </div>
      ) : null}
    </div>
  );
}