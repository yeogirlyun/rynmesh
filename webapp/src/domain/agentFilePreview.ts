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
export type OfficePreviewLoader = (signal: AbortSignal) => Promise<FilePreview>;

/** Windows text exports often use a UTF-16 BOM or GB18030 instead of UTF-8. */
export async function decodeAgentFileText(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  if (bytes[0] === 0xff && bytes[1] === 0xfe) return new TextDecoder("utf-16le").decode(bytes);
  if (bytes[0] === 0xfe && bytes[1] === 0xff) return new TextDecoder("utf-16be").decode(bytes);
  try { return new TextDecoder("utf-8", { fatal: true }).decode(bytes); }
  catch { return new TextDecoder("gb18030").decode(bytes); }
}

export async function readAgentFilePreview(taskID: string, file: Pick<AgentFileArtifact, "id" | "sha256">, signal: AbortSignal): Promise<FilePreview> {
  signal.throwIfAborted();
  const url = new URL(agentFileUrl(taskID, file), window.location.href);
  url.pathname += "/preview";
  const controller = new AbortController();
  const cancel = () => controller.abort(signal.reason);
  signal.addEventListener("abort", cancel, { once: true });
  const timer = setTimeout(() => controller.abort(new AgentAPIError("agent_preview_timeout", 0)), 30000);
  try {
    const response = await fetch(url.toString(), { credentials: "include", cache: "no-store", signal: controller.signal });
    const value = await response.json();
    if (!response.ok) throw new AgentAPIError(typeof value.detail === "string" ? value.detail : "agent_preview_invalid", response.status);
    if (file.sha256 && value.sha256 && file.sha256 !== value.sha256) throw new AgentAPIError("agent_artifact_content_changed", 409);
    return value as FilePreview;
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", cancel);
  }
}
