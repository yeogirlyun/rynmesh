import { useContext, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { File, FileSpreadsheet, FileText, X } from "lucide-react";
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
  const [lightbox, setLightbox] = useState<{ src: string; name: string } | null>(null);

  useEffect(() => {
    if (!lightbox) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setLightbox(null);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [lightbox]);

  if (!attachments?.length) return null;
  return (
    <>
      <div className="ra-message-attachments">
        {attachments.map(file => {
          const info = attachmentFormat(file.name, file.media_type);
          const sizeText = formatAttachmentBytes(file.size);
          const subtitle = [info.label, sizeText].filter(Boolean).join(" · ");
          const bound = fileContext ? findAgentFile(fileContext.files, file.path || file.name) : undefined;
          const canOpenBound = Boolean(bound && fileAvailable(bound));
          const isImage = Boolean(file.image_url);
          const canInteract = isImage || canOpenBound;
          const Icon = info.kind === "sheet" ? FileSpreadsheet : info.kind === "pdf" || info.kind === "doc" ? FileText : File;
          const handleActivate = () => {
            if (isImage && file.image_url) {
              setLightbox({ src: file.image_url, name: file.name });
            } else if (canOpenBound && bound) {
              fileContext?.open(bound);
            }
          };
          if (isImage && file.image_url) {
            return (
              <div
                className="ra-message-attachment ra-message-attachment-tile is-openable"
                key={file.id}
                title={file.path || file.name}
                role="button"
                tabIndex={0}
                onClick={handleActivate}
                onKeyDown={event => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    handleActivate();
                  }
                }}
              >
                <img src={file.image_url} alt={file.name} />
              </div>
            );
          }
          return (
            <div
              className={`ra-message-attachment${canInteract ? " is-openable" : ""}`}
              key={file.id}
              title={file.path || file.name}
              role={canInteract ? "button" : undefined}
              tabIndex={canInteract ? 0 : undefined}
              onClick={canInteract ? handleActivate : undefined}
              onKeyDown={
                canInteract
                  ? event => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        handleActivate();
                      }
                    }
                  : undefined
              }
            >
              <span className="ra-message-attachment-badge" data-kind={info.kind}>
                <Icon size={18} strokeWidth={1.8} />
              </span>
              <div className="ra-message-attachment-meta">
                <span className="ra-message-attachment-name">{file.name}</span>
                <small className="ra-message-attachment-sub">
                  <span className="ra-attachment-sub-default">{subtitle}</span>
                  {canInteract && <span className="ra-attachment-sub-hover">{t("openFile")}</span>}
                </small>
              </div>
            </div>
          );
        })}
      </div>
      {lightbox &&
        createPortal(
          <div
            className="ra-image-lightbox"
            role="dialog"
            aria-modal="true"
            aria-label={lightbox.name}
            onClick={() => setLightbox(null)}
          >
            <div className="ra-image-lightbox-inner" onClick={e => e.stopPropagation()}>
              <div className="ra-image-lightbox-bar">
                <span>{lightbox.name}</span>
                <button
                  type="button"
                  className="ra-image-lightbox-close"
                  aria-label={t("close")}
                  onClick={() => setLightbox(null)}
                >
                  <X size={18} />
                </button>
              </div>
              <img src={lightbox.src} alt={lightbox.name} />
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}
