import { useEffect, useState } from "react";
import { Box, Button, Group, Modal, Select, Stack, Text, Textarea, TextInput } from "@mantine/core";
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
    <Modal
      opened={open}
      onClose={() => onOpenChange(false)}
      title="下发任务指令"
      centered
      size="lg"
      radius="xl"
      overlayProps={{ backgroundOpacity: 0.55, blur: 2 }}
      styles={{
        content: {
          background: "rgba(17,18,26,.88)",
          backdropFilter: "blur(26px) saturate(150%)",
          WebkitBackdropFilter: "blur(26px) saturate(150%)",
          border: "1px solid rgba(255,255,255,.08)",
        },
      }}
    >
      <Box component="form" onSubmit={submit}>
        <Stack gap="md">
          <Text size="xs" c="dimmed" lh={1.5}>
            写清要做什么和验收标准，任务会走流水线审批后再派单。
          </Text>
          <Textarea
            size="sm"
            rows={5}
            data-autofocus
            id="composer-text"
            name="title"
            label="指令 / 标题"
            placeholder="要做什么、验收标准是什么。会按任务流水线走审批再派单。"
            value={title}
            onChange={(event) => setTitle(event.currentTarget.value)}
          />
          <Select
            size="sm"
            id="composer-project"
            name="project"
            label="项目"
            placeholder="默认（不指定）"
            data={projects.map((name) => ({ value: name, label: name }))}
            value={project || null}
            onChange={(next) => setProject(next || "")}
            clearable
            comboboxProps={{ withinPortal: true }}
          />
          <Text size="xs" c="dimmed">写操作需要 API Token（右上角设置一次，保存在本机浏览器）。</Text>
          <Group justify="flex-end" gap="xs" mt="xs">
            <Button variant="subtle" onClick={() => onOpenChange(false)}>取消</Button>
            <Button type="submit" disabled={busy || !title.trim()} loading={busy}>
              创建任务
            </Button>
          </Group>
        </Stack>
      </Box>
    </Modal>
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
    <Modal
      opened={open}
      onClose={() => onOpenChange(false)}
      title="API Token"
      centered
      radius="xl"
      overlayProps={{ backgroundOpacity: 0.55, blur: 2 }}
      styles={{
        content: {
          background: "rgba(17,18,26,.88)",
          backdropFilter: "blur(26px) saturate(150%)",
          WebkitBackdropFilter: "blur(26px) saturate(150%)",
          border: "1px solid rgba(255,255,255,.08)",
        },
      }}
    >
      <Stack gap="md">
        <Text size="xs" c="dimmed" lh={1.5}>
          新建任务、发指令、唤醒或取消任务需要它；只存在这台浏览器里。
        </Text>
        <TextInput
          size="sm"
          id="token-input"
          name="token"
          label="X-Hub-Token"
          type="password"
          autoComplete="off"
          data-autofocus
          placeholder="服务端 config 里的 server.api_token"
          value={value}
          onChange={(event) => setValue(event.currentTarget.value)}
        />
        <Group justify="space-between" mt="xs">
          <Button
            variant="subtle"
            color="red"
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
        </Group>
      </Stack>
    </Modal>
  );
}