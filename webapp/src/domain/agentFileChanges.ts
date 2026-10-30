import { nodeControlUrl } from "./nodeUrl";

export interface FileChange {
  id: string; path: string; status: string; source: string; can_undo: boolean;
  binary?: boolean; diff?: string; diff_truncated?: boolean; rename_group?: boolean;
  before_bytes: number; after_bytes: number; media_type: string;
  problem?: string; conflict?: string;
}
export interface UndoResult {
  id?: string; status: string; completed: string[];
  remaining?: { path: string; state: string }[];
  conflicts?: { path: string; reason: string }[];
}
export interface ChangeReview {
  files: FileChange[]; task_busy?: boolean;
  observed?: { path: string; status: string }[];
  undo_operations?: UndoResult[];
}
export interface UndoPreview {
  token: string; can_undo: boolean;
  items: { path: string; before: { bytes: number; sha256: string | null }; after: { bytes: number; sha256: string | null } }[];
  conflicts: { path: string; reason: string }[];
}
async function request<T>(task: string, action = "", body?: unknown): Promise<T> {
  const response = await fetch(nodeControlUrl(`/agent/tasks/${encodeURIComponent(task)}/changes${action}`), {
    method: body === undefined ? "GET" : "POST", credentials: "include", cache: "no-store",
    headers: { "Content-Type": "application/json" }, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "file_review_unavailable");
  return data;
}
export const agentFileChanges = {
  review: (task: string) => request<ChangeReview>(task),
  preview: (task: string, ids?: string[]) => request<UndoPreview>(task, "/preview", { ids }),
  undo: (task: string, token: string, request_id: string, ids?: string[]) =>
    request<UndoResult>(task, "/undo", { ids, token, request_id, confirm: true }),
};
