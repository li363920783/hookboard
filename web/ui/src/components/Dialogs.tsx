import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { clearToken, getToken, saveToken } from "@/lib/api";

export function ComposerDialog({
  open,
  projects,
  onOpenChange,
  onCreate,
}: {
  open: boolean;
  projects: string[];
  onOpenChange: (open: boolean) => void;
  onCreate: (title: string, project: string) => Promise<boolean>;
}) {
  const [title, setTitle] = useState("");
  const [project, setProject] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setTitle("");
      setProject("");
    }
  }, [open]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!title.trim()) return;
    setBusy(true);
    try {
      const ok = await onCreate(title.trim(), project);
      if (ok) onOpenChange(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={submit} className="grid gap-4">
          <DialogHeader>
            <DialogTitle>下发任务指令</DialogTitle>
            <DialogDescription>
              写清要做什么和验收标准，任务会走流水线审批后再派单。
            </DialogDescription>
          </DialogHeader>
          <label className="grid gap-1.5">
            <span className="text-muted-foreground text-xs">指令 / 标题</span>
            <Textarea
              rows={5}
              autoFocus
              id="composer-text"
              name="title"
              placeholder="要做什么、验收标准是什么。会按任务流水线走审批再派单。"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
            />
          </label>
          <label className="grid gap-1.5">
            <span className="text-muted-foreground text-xs">项目</span>
            <Select id="composer-project" name="project" value={project} onChange={(event) => setProject(event.target.value)} className="w-full">
              <option value="">默认（不指定）</option>
              {projects.map((name) => (
                <option key={name} value={name}>{name}</option>
              ))}
            </Select>
          </label>
          <p className="text-muted-foreground text-xs">
            写操作需要 API Token（右上角设置一次，保存在本机浏览器）。
          </p>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>取消</Button>
            <Button type="submit" disabled={busy || !title.trim()}>
              {busy ? "创建中…" : "创建任务"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function SettingsDialog({
  open,
  onOpenChange,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: (message: string) => void;
}) {
  const [value, setValue] = useState("");
  useEffect(() => {
    if (open) setValue(getToken());
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>API Token</DialogTitle>
          <DialogDescription>
            新建任务、发指令、唤醒或取消任务需要它；只存在这台浏览器里。
          </DialogDescription>
        </DialogHeader>
        <label className="grid gap-1.5">
          <span className="text-muted-foreground text-xs">X-Hub-Token</span>
          <Input
            id="token-input"
            name="token"
            type="password"
            autoComplete="off"
            placeholder="服务端 config 里的 server.api_token"
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
        </label>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => {
              clearToken();
              onOpenChange(false);
              onSaved("Token 已清除");
            }}
          >
            清除
          </Button>
          <Button
            onClick={() => {
              saveToken(value);
              onOpenChange(false);
              onSaved("Token 已保存");
            }}
          >
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}