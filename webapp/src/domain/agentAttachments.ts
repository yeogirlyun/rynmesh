import * as dropDesktop from "./dropDesktop";
import { nodeControlUrl } from "./nodeUrl";

/** User-selected files travel with the request, including native desktop drops. */
export interface AgentAttachmentInput {
  name: string;
  media_type?: string;
  data_base64?: string;
  path?: string;
}

export interface AgentDraftAttachment extends AgentAttachmentInput {
  id: string;
  size?: number;
  preview?: string;
  reference_only?: boolean;
}

export interface AgentMessageAttachment {
  id: string;
  name: string;
  media_type: string;
  size: number;
  path: string;
  sha256: string;
  image_url?: string;
  reference_only?: boolean;
}

export interface AgentAttachmentLimits {
  count: number;
  images: number;
  fileBytes: number;
  totalBytes: number;
}

export interface ServerAttachmentLimits {
  count?: number;
  images?: number;
  file_bytes?: number;
  total_bytes?: number;
}

export const DEFAULT_AGENT_ATTACHMENT_LIMITS: AgentAttachmentLimits = {
  count: 8,
  images: 4,
  fileBytes: 10 * 1024 * 1024,
  totalBytes: 20 * 1024 * 1024,
};

export const AGENT_ATTACHMENT_LIMITS: AgentAttachmentLimits = { ...DEFAULT_AGENT_ATTACHMENT_LIMITS };

export function applyAgentAttachmentLimits(limits?: ServerAttachmentLimits | null): AgentAttachmentLimits {
  const next: AgentAttachmentLimits = {
    count: typeof limits?.count === "number" && limits.count > 0 ? limits.count : DEFAULT_AGENT_ATTACHMENT_LIMITS.count,
    images: typeof limits?.images === "number" && limits.images > 0 ? limits.images : DEFAULT_AGENT_ATTACHMENT_LIMITS.images,
    fileBytes: typeof limits?.file_bytes === "number" && limits.file_bytes > 0 ? limits.file_bytes : DEFAULT_AGENT_ATTACHMENT_LIMITS.fileBytes,
    totalBytes: typeof limits?.total_bytes === "number" && limits.total_bytes > 0 ? limits.total_bytes : DEFAULT_AGENT_ATTACHMENT_LIMITS.totalBytes,
  };
  Object.assign(AGENT_ATTACHMENT_LIMITS, next);
  return next;
}

export async function loadAgentAttachmentLimits(signal?: AbortSignal): Promise<AgentAttachmentLimits> {
  try {
    const response = await fetch(nodeControlUrl("/agent/settings"), {
      method: "GET",
      credentials: "include",
      cache: "no-store",
      signal,
    });
    if (!response.ok) return applyAgentAttachmentLimits(null);
    const data = (await response.json()) as { attachment_limits?: ServerAttachmentLimits };
    return applyAgentAttachmentLimits(data.attachment_limits);
  } catch {
    return applyAgentAttachmentLimits(null);
  }
}

export type AgentAttachmentLimitKind = "count" | "images" | "file_bytes" | "total_bytes";

export interface AgentAttachmentLimitDetails {
  limit: AgentAttachmentLimitKind;
  name: string;
  max: number;
}

export class AttachmentLimitError extends Error {
  readonly details: AgentAttachmentLimitDetails;
  constructor(details: AgentAttachmentLimitDetails) {
    super("attachmentLimit");
    this.name = "AttachmentLimitError";
    this.details = details;
  }
}

export function formatLimitMegabytes(bytes: number): number {
  return Math.round((bytes / (1024 * 1024)) * 10) / 10;
}

export function attachmentLimitTranslation(
  details?: Partial<AgentAttachmentLimitDetails> | null,
  limits: AgentAttachmentLimits = AGENT_ATTACHMENT_LIMITS,
): { key: string; values: Record<string, string | number> } {
  const baseValues = {
    count: limits.count,
    images: limits.images,
    fileMB: formatLimitMegabytes(limits.fileBytes),
    totalMB: formatLimitMegabytes(limits.totalBytes),
  };
  if (!details?.limit || typeof details.max !== "number") {
    return { key: "attachmentLimit", values: baseValues };
  }
  const name = details.name || "";
  if (details.limit === "count" && name) {
    return { key: "attachmentLimit_count", values: { name, max: details.max } };
  }
  if (details.limit === "images" && name) {
    return { key: "attachmentLimit_images", values: { name, max: details.max } };
  }
  if (details.limit === "file_bytes" && name) {
    return { key: "attachmentLimit_file_bytes", values: { name, maxMB: formatLimitMegabytes(details.max) } };
  }
  if (details.limit === "total_bytes" && name) {
    return { key: "attachmentLimit_total_bytes", values: { name, maxMB: formatLimitMegabytes(details.max) } };
  }
  return { key: "attachmentLimit", values: baseValues };
}

export function attachmentIsImage(file: Pick<AgentAttachmentInput, "name" | "media_type">): boolean {
  return Boolean(file.media_type?.startsWith("image/") || /\.(png|jpe?g|webp|gif|bmp|tiff?)$/i.test(file.name));
}

export function validateAgentAttachments(
  files: Array<Pick<AgentDraftAttachment, "name" | "media_type" | "size" | "path" | "reference_only">>,
  limits: AgentAttachmentLimits = AGENT_ATTACHMENT_LIMITS,
): void {
  if (files.length > limits.count) {
    const offending = files[limits.count] || files[files.length - 1];
    throw new AttachmentLimitError({ limit: "count", name: offending?.name || "", max: limits.count });
  }
  let images = 0;
  let total = 0;
  for (const file of files) {
    const size = file.size || 0;
    const isRefOnly = Boolean(file.reference_only || (file.path && size > limits.fileBytes));
    if (isRefOnly) continue;
    if (size > limits.fileBytes) {
      throw new AttachmentLimitError({ limit: "file_bytes", name: file.name, max: limits.fileBytes });
    }
    total += size;
    if (total > limits.totalBytes) {
      throw new AttachmentLimitError({ limit: "total_bytes", name: file.name, max: limits.totalBytes });
    }
    if (attachmentIsImage(file)) {
      images += 1;
      if (images > limits.images) {
        throw new AttachmentLimitError({ limit: "images", name: file.name, max: limits.images });
      }
    }
  }
}

function readFile(file: File): Promise<AgentDraftAttachment> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("attachmentReadError"));
    reader.onabort = () => reject(new Error("attachmentReadError"));
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string" || !result.includes(",")) { reject(new Error("attachmentReadError")); return; }
      resolve({ id: crypto.randomUUID(), name: file.name || "pasted-image.png", size: file.size,
        media_type: file.type || "application/octet-stream", data_base64: result.slice(result.indexOf(",") + 1),
        ...(file.type.startsWith("image/") ? { preview: result } : {}) });
    };
    reader.readAsDataURL(file);
  });
}

export async function readAgentAttachments(files: File[], existing: AgentDraftAttachment[] = []): Promise<AgentDraftAttachment[]> {
  validateAgentAttachments([...existing, ...files.map(file => ({ name: file.name, media_type: file.type, size: file.size }))]);
  return Promise.all(files.map(readFile));
}

export async function nativeAgentAttachments(
  paths: string[],
  existing: AgentDraftAttachment[] = [],
  options?: { onDirectories?: (directories: string[]) => void },
): Promise<AgentDraftAttachment[]> {
  const inspected = typeof dropDesktop.inspectDropPaths === "function"
    ? await dropDesktop.inspectDropPaths(paths)
    : paths.map(path => ({ path, size: 0, kind: "file" as const }));
  const directories: string[] = [];
  const added: AgentDraftAttachment[] = [];
  for (const info of inspected) {
    if (info.kind === "missing") throw new Error("attachmentReadError");
    if (info.kind === "dir") {
      directories.push(info.path);
      continue;
    }
    const name = info.path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || info.path;
    const referenceOnly = info.size > AGENT_ATTACHMENT_LIMITS.fileBytes;
    added.push({
      id: crypto.randomUUID(),
      path: info.path,
      name,
      size: info.size,
      ...(referenceOnly ? { reference_only: true } : {}),
    });
  }
  if (added.length) {
    validateAgentAttachments([...existing, ...added]);
  }
  if (directories.length) {
    options?.onDirectories?.(directories);
  }
  return added;
}

export function agentAttachmentInputs(files: AgentDraftAttachment[]): AgentAttachmentInput[] {
  return files.map(({ name, media_type, data_base64, path }) => ({ name, ...(media_type ? { media_type } : {}),
    ...(path ? { path } : { data_base64 }) }));
}
