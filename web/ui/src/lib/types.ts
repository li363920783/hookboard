export type Status = "pending" | "running" | "waiting" | "done" | "failed" | "cancelled";

export interface Approval {
  token: string;
  stage?: string;
  question: string;
  options: string[];
  recommend?: number;
  status?: "open" | "superseded" | "decided" | string;
  answer_index?: number;
  answer_text?: string;
  by?: string;
  created_at?: number;
  decided_at?: number;
}

export interface Task {
  id: number;
  source: string;
  kind?: string;
  external_id: string;
  title: string;
  status: Status;
  stage: string;
  note?: string;
  context?: string;
  session?: string;
  workspace?: string;
  branch?: string;
  project?: string;
  attempts?: number;
  created_at: number;
  updated_at?: number;
  payload?: unknown;
  approval?: Approval | null;
}

export interface Column {
  key: string;
  title: string;
  tasks: Task[];
}

export interface BoardData {
  ok?: boolean;
  columns: Column[];
  projects?: string[];
}

export interface Run {
  stage: string;
  agent: string;
  ok: boolean;
  output?: string;
  started_at?: number;
  finished_at?: number;
}

export interface TaskEvent {
  ts: number;
  kind: string;
  detail?: string;
}

export interface Detail {
  task: Task;
  approvals: Approval[];
  events: TaskEvent[];
  runs: Run[];
}