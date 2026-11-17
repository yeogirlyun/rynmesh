import { nodeControlUrl } from "./nodeUrl";

export interface FileChange {
  id: string; path: string; status: string; source: string; can_undo: boolean;
  binary?: boolean; diff?: string; diff_truncated?: boolean; rename_group?: boolean; snapshot?: boolean;
  before_bytes: number; after_bytes: number; media_type: string;
  problem?: string; conflict?: string;
  state?: string; restore_available?: boolean;
}
export interface SkippedUndoItem {
  path: string; reason: string; can_undo?: boolean;
}
export interface UndoResult {
  id?: string; status: string; completed: string[];
  remaining?: { path: string; state: string }[];
  conflicts?: { path: string; reason: string }[];
  skipped?: SkippedUndoItem[];
}
export interface ChangeReview {
  files: FileChange[]; task_busy?: boolean; snapshot_skipped?: number;
  observed?: { path: string; status: string }[];
  undo_operations?: UndoResult[];
}
export interface UndoPreview {
  token: string; can_undo: boolean;
  items: { path: string; before: { bytes: number; sha256: string | null }; after: { bytes: number; sha256: string | null } }[];
  conflicts: { path: string; reason: string }[];
  skipped?: SkippedUndoItem[];
}

export const FILE_REVIEW_TIMEOUT_MS = 30000;

async function request<T>(task: string, action = "", body?: unknown): Promise<T> {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort(new Error("file_review_timeout"));
  }, FILE_REVIEW_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(nodeControlUrl(`/agent/tasks/${encodeURIComponent(task)}/changes${action}`), {
      method: body === undefined ? "GET" : "POST",
      credentials: "include",
      cache: "no-store",
      signal: controller.signal,
      headers: { "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch (err) {
    clearTimeout(timer);
    if (timedOut || controller.signal.aborted) {
      throw new Error("file_review_timeout");
    }
    throw err instanceof Error ? err : new Error(String(err));
  }
  try {
    const rawText = await response.text();
    let parsed: Record<string, unknown> | null = null;
    if (rawText) {
      try {
        const val = JSON.parse(rawText);
        if (val && typeof val === "object") parsed = val as Record<string, unknown>;
      } catch {
        parsed = null;
      }
    }
    if (!response.ok) {
      const detail = typeof parsed?.detail === "string" && parsed.detail ? parsed.detail : "";
      throw new Error(detail || `HTTP ${response.status}`);
    }
    if (parsed === null) {
      throw new Error(`HTTP ${response.status}: invalid_json`);
    }
    return parsed as T;
  } catch (err) {
    if (timedOut || controller.signal.aborted) {
      throw new Error("file_review_timeout");
    }
    throw err instanceof Error ? err : new Error(String(err));
  } finally {
    clearTimeout(timer);
  }
}
export const agentFileChanges = {
  review: (task: string) => request<ChangeReview>(task),
  preview: (task: string, ids?: string[]) => request<UndoPreview>(task, "/preview", { ids }),
  undo: (task: string, token: string, request_id: string, ids?: string[]) =>
    request<UndoResult>(task, "/undo", { ids, token, request_id, confirm: true }),
};
