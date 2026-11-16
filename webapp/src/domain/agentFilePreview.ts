import { AgentAPIError, agentFileUrl, type AgentFileArtifact } from "./rynAgent";

export interface PreviewCell { column: number; value: string; formula?: string }
export interface PreviewSheet {
  name: string; rows: { index: number; cells: PreviewCell[] }[]; columns: number;
  total_rows: number; total_columns: number; truncated: boolean; hidden?: boolean;
}
export type PreviewBlock = { type: "paragraph"; style: string; runs: { text: string; bold: boolean; italic: boolean }[] }
  | { type: "table"; rows: string[][] } | { type: "image"; url: string };
export type FilePreview = { truncated: boolean; sha256?: string } & (
  { kind: "spreadsheet"; sheets: PreviewSheet[]; formulas_uncalculated?: boolean }
  | { kind: "document"; blocks: PreviewBlock[] }
  | { kind: "presentation"; slides: { number: number; paragraphs: string[]; images?: string[] }[]; total_slides: number }
);
export const isOfficePreview = (name: string) => /\.(xlsx|xlsm|xltx|xls|csv|tsv|docx|docm|doc|pptx|pptm|ppt|odt|ods|odp|rtf)$/i.test(name);
export type PreviewStatus = "loading" | "converting";
export interface OfficePreviewOptions { onStatus?: (status: PreviewStatus) => void }
export type OfficePreviewLoader = (signal: AbortSignal, options?: OfficePreviewOptions) => Promise<FilePreview>;
export const PREVIEW_POLL_INTERVAL_MS = 2000;
export const PREVIEW_CONVERT_MAX_WAIT_MS = 180_000;

/** Windows text exports often use a UTF-16 BOM or GB18030 instead of UTF-8. */
export async function decodeAgentFileText(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  if (bytes[0] === 0xff && bytes[1] === 0xfe) return new TextDecoder("utf-16le").decode(bytes);
  if (bytes[0] === 0xfe && bytes[1] === 0xff) return new TextDecoder("utf-16be").decode(bytes);
  try { return new TextDecoder("utf-8", { fatal: true }).decode(bytes); }
  catch { return new TextDecoder("gb18030").decode(bytes); }
}

function waitAbortable(ms: number, signal: AbortSignal): Promise<void> {
  signal.throwIfAborted();
  return new Promise<void>((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

export async function readAgentFilePreview(
  taskID: string,
  file: Pick<AgentFileArtifact, "id" | "sha256"> & { bytes?: number },
  signal: AbortSignal,
  options?: OfficePreviewOptions,
): Promise<FilePreview> {
  signal.throwIfAborted();
  const url = new URL(agentFileUrl(taskID, file), window.location.href);
  url.pathname += "/preview";
  const requestTimeoutMs = Math.min(300_000, Math.max(30_000, Math.ceil((file.bytes ?? 0) / (256 * 1024)) * 1000));
  const startedAt = Date.now();
  for (;;) {
    signal.throwIfAborted();
    const controller = new AbortController();
    const cancel = () => controller.abort(signal.reason);
    signal.addEventListener("abort", cancel, { once: true });
    const timer = setTimeout(() => controller.abort(new AgentAPIError("agent_preview_timeout", 0)), requestTimeoutMs);
    let response: Response;
    let value: unknown;
    try {
      response = await fetch(url.toString(), { credentials: "include", cache: "no-store", signal: controller.signal });
      value = await response.json();
    } finally {
      clearTimeout(timer);
      signal.removeEventListener("abort", cancel);
    }
    const record = (value && typeof value === "object" ? value : {}) as Record<string, unknown>;
    if (response.status === 202 || record.status === "converting") {
      options?.onStatus?.("converting");
      const elapsed = Date.now() - startedAt;
      if (elapsed >= PREVIEW_CONVERT_MAX_WAIT_MS) {
        throw new AgentAPIError("agent_preview_timeout", 0);
      }
      await waitAbortable(Math.min(PREVIEW_POLL_INTERVAL_MS, PREVIEW_CONVERT_MAX_WAIT_MS - elapsed), signal);
      continue;
    }
    if (!response.ok) throw new AgentAPIError(typeof record.detail === "string" ? record.detail : "agent_preview_invalid", response.status);
    if (file.sha256 && record.sha256 && file.sha256 !== record.sha256) throw new AgentAPIError("agent_artifact_content_changed", 409);
    return value as FilePreview;
  }
}
