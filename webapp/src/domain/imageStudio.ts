import { nodeControlUrl } from "./nodeUrl";
import type { Audience } from "./nodeSites";

/** Owner view of the image studio this computer hosts for colleagues. */
export interface StudioStatus {
  model: { ready: boolean; code: string };
  enabled: boolean;
  audience: Audience;
  queue_length: number;
  running: string | null;
  today: number;
  average_seconds: number;
}
export interface StudioJob {
  id: string;
  purpose: string;
  width: number;
  height: number;
  brief: string;
  words: string;
  state: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  failure: string | null;
  created_at: number;
  started_at: number | null;
  position?: number;
  caller_name: string;
  caller: string;
  priority: number;
}
export interface StudioUsage { caller: string; name: string; today: number; last_30_days: number; failed: number }

export class StudioError extends Error {
  constructor(readonly code: string, readonly status: number) { super(code); }
}

async function call<T>(path: string, method = "GET"): Promise<T> {
  const response = await fetch(nodeControlUrl(`/image-studio${path}`), { method, credentials: "include", cache: "no-store" });
  let body: unknown = null;
  try { body = await response.json(); } catch { body = null; }
  if (!response.ok) {
    const code = body && typeof body === "object" && typeof (body as { code?: unknown }).code === "string"
      ? (body as { code: string }).code : "image_unavailable";
    throw new StudioError(code, response.status);
  }
  return body as T;
}

export const imageStudio = {
  status: () => call<StudioStatus>("/status"),
  queue: () => call<{ jobs: StudioJob[] }>("/queue").then(value => value.jobs),
  usage: () => call<{ rows: StudioUsage[] }>("/usage").then(value => value.rows),
  cancel: (id: string) => call<StudioJob>(`/jobs/${encodeURIComponent(id)}/cancel`, "POST"),
  prioritize: (id: string) => call<StudioJob>(`/jobs/${encodeURIComponent(id)}/prioritize`, "POST"),
};

export const IMAGE_STUDIO_SITE_ID = "image-studio";
