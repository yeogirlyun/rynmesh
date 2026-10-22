import { useContext } from "react";
import { File, FileSpreadsheet, FileText } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { AgentMessageAttachment } from "../domain/agentAttachments";
import { AgentFileContext, fileAvailable, findAgentFile } from "./RynAgentFileContext";
import "./RynAgentComposer.css";

function formatAttachmentBytes(bytes?: number): string {
  if (!bytes || bytes <= 0) return "";
  return bytes < 1048576 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
}

function attachmentFormat(name: string, mediaType?: string): { label: string; kind: "pdf" | "sheet" | "doc" | "file" } {
  const ext = name.includes(".") ? name.split(".").pop()?.toLowerCase() || "" : "";
  if (ext === "pdf" || mediaType === "application/pdf") return { label: "PDF", kind: "pdf" };
  if (/^(xlsx?|xlsm|csv|tsv)$/.test(ext)) return { label: ext.toUpperCase(), kind: "sheet" };
  if (/^(docx?|md|txt|rtf)$/.test(ext)) return { label: ext.toUpperCase(), kind: "doc" };
  return { label: ext ? ext.toUpperCase() : "FILE", kind: "file" };
}

/** Render the server's persisted attachment metadata above the owner message bubble (Codex style). */
export default function AgentMessageAttachments({ attachments }: { attachments?: AgentMessageAttachment[] }) {
  const { t } = useTranslation("rynAgent");
  const fileContext = useContext(AgentFileContext);
  if (!attachments?.length) return null;
  return <div className="ra-message-attachments">{attachments.map(file => {
    const info = attachmentFormat(file.name, file.media_type);
    const sizeText = formatAttachmentBytes(file.size);
    const subtitle = [info.label, sizeText].filter(Boolean).join(" · ");
    const bound = fileContext ? findAgentFile(fileContext.files, file.path || file.name) : undefined;
    const canOpen = Boolean(bound && fileAvailable(bound));
    const Icon = info.kind === "sheet" ? FileSpreadsheet : info.kind === "pdf" || info.kind === "doc" ? FileText : File;
    return <div
      className={`ra-message-attachment${canOpen ? " is-openable" : ""}`}
      key={file.id}
      title={file.path || file.name}
      role={canOpen ? "button" : undefined}
      tabIndex={canOpen ? 0 : undefined}
      onClick={canOpen && bound ? () => fileContext?.open(bound) : undefined}
      onKeyDown={canOpen && bound ? event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          fileContext?.open(bound);
        }
      } : undefined}
    >
      {file.image_url ? <img src={file.image_url} alt={file.name} /> : (
        <span className="ra-message-attachment-badge" data-kind={info.kind}>
          <Icon size={18} strokeWidth={1.8} />
        </span>
      )}
      <div className="ra-message-attachment-meta">
        <span className="ra-message-attachment-name">{file.name}</span>
        <small className="ra-message-attachment-sub">
          <span className="ra-attachment-sub-default">{subtitle}</span>
          {canOpen && <span className="ra-attachment-sub-hover">{t("openFile")}</span>}
        </small>
      </div>
    </div>;
  })}</div>;
}
