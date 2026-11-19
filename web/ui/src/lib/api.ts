import type { BoardData, Detail } from "./types";

const TOKEN_KEY = "hookboard_token";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function saveToken(value: string) {
  localStorage.setItem(TOKEN_KEY, value.trim());
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

export class Unauthorized extends Error {}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { cache: "no-store", ...init });
  if (response.status === 401) throw new Unauthorized("API Token 无效");
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || data.detail || `HTTP ${response.status}`);
  }
  return data as T;
}

export function fetchBoard(): Promise<BoardData> {
  return request<BoardData>("/api/board");
}

export function fetchDetail(id: number): Promise<Detail> {
  return request<Detail>(`/api/tasks/${id}`);
}

export async function writeJSON<T = { ok: boolean }>(url: string, payload: unknown): Promise<T> {
  if (!getToken()) throw new Unauthorized("先设置 API Token");
  return request<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Hub-Token": getToken() },
    body: JSON.stringify(payload),
  });
}

export function decide(token: string, payload: { index?: number; text?: string }) {
  return request<{ ok: boolean; changed?: boolean }>(`/a/${encodeURIComponent(token)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(payload),
  });
}