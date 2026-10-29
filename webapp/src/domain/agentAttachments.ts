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
}

export interface AgentMessageAttachment {
  id: string;
  name: string;
  media_type: string;
  size: number;
  path: string;
  sha256: string;
  image_url?: string;
}

export const AGENT_ATTACHMENT_LIMITS = { count: 8, images: 4, fileBytes: 10 * 1024 * 1024, totalBytes: 20 * 1024 * 1024 };

export function attachmentIsImage(file: Pick<AgentAttachmentInput, "name" | "media_type">): boolean {
  return Boolean(file.media_type?.startsWith("image/") || /\.(png|jpe?g|webp|gif|bmp|tiff?)$/i.test(file.name));
}

export function validateAgentAttachments(files: Array<Pick<AgentDraftAttachment, "name" | "media_type" | "size">>): void {
  if (files.length > AGENT_ATTACHMENT_LIMITS.count
      || files.filter(attachmentIsImage).length > AGENT_ATTACHMENT_LIMITS.images
      || files.some(file => (file.size || 0) > AGENT_ATTACHMENT_LIMITS.fileBytes)
      || files.reduce((total, file) => total + (file.size || 0), 0) > AGENT_ATTACHMENT_LIMITS.totalBytes) {
    throw new Error("attachmentLimit");
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

export function nativeAgentAttachments(paths: string[], existing: AgentDraftAttachment[] = []): AgentDraftAttachment[] {
  const added = paths.map(path => ({ id: crypto.randomUUID(), path, name: path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path }));
  validateAgentAttachments([...existing, ...added]);
  return added;
}

export function agentAttachmentInputs(files: AgentDraftAttachment[]): AgentAttachmentInput[] {
  return files.map(({ name, media_type, data_base64, path }) => ({ name, ...(media_type ? { media_type } : {}),
    ...(path ? { path } : { data_base64 }) }));
}
