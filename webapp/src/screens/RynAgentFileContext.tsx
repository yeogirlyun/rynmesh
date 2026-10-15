import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { FileText, Image as ImageIcon } from "lucide-react";
import { agentFileUrl, rynAgent, type AgentFileArtifact } from "../domain/rynAgent";

export const AgentFileContext = createContext<{ taskID: string; files: AgentFileArtifact[]; open: (file: AgentFileArtifact) => void } | null>(null);
export const fileAvailable = (file: AgentFileArtifact) => file.status === "current" || file.status === "changed_outside_task" || file.status === "unverified";

function normalize(value: string): string {
  try { value = decodeURIComponent(value); } catch { /* Literal percent signs are valid in filenames. */ }
  value = value.replace(/^sandbox:/i, "").replace(/^file:\/\/\/?/i, "").replace(/\\/g, "/").replace(/:\d+(?::\d+)?$/, "");
  if (/^\/[a-z]:\//i.test(value)) value = value.slice(1);
  return /^[a-z]:\//i.test(value) ? value.toLowerCase() : value;
}

export function findAgentFile(files: AgentFileArtifact[], value: string): AgentFileArtifact | undefined {
  if (/^(?:https?|mailto|data|javascript):/i.test(value)) return;
  const wanted = normalize(value);
  const exact = files.filter(file => [file.absolute_path, file.path].some(path => path && normalize(path) === wanted));
  if (exact.length === 1) return exact[0];
  const named = files.filter(file => normalize(file.name) === wanted);
  return named.length === 1 ? named[0] : undefined;
}

export function AgentFileLink({ file, children }: { file: AgentFileArtifact; children: ReactNode }) {
  const context = useContext(AgentFileContext);
  const path = file.absolute_path || file.path;
  if (!context || !fileAvailable(file)) return <span title={path}>{children}</span>;
  return <a className="ra-file-link" href={agentFileUrl(context.taskID, file)} title={path}
    onClick={event => { event.preventDefault(); context.open(file); }}><FileText size={14} aria-hidden="true" />{children}</a>;
}

export function AgentFileImage({ file, alt }: { file: AgentFileArtifact; alt?: string }) {
  const context = useContext(AgentFileContext);
  const [url, setURL] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    setURL(""); setFailed(false);
    if (!context || !fileAvailable(file)) return;
    const controller = new AbortController(); let objectURL = "";
    void rynAgent.fileContent(context.taskID, file, controller.signal).then(blob => {
      if (!controller.signal.aborted) { objectURL = URL.createObjectURL(blob); setURL(objectURL); }
    }).catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => { controller.abort(); if (objectURL) URL.revokeObjectURL(objectURL); };
  }, [context?.taskID, file.id, file.sha256, file.status]);
  if (!context || failed || !fileAvailable(file)) return <AgentFileLink file={file}>{alt || file.name}</AgentFileLink>;
  return <button className="ra-inline-image" title={file.absolute_path || file.path} aria-label={alt || file.name} onClick={() => context.open(file)}>
    {url ? <img src={url} alt={alt || file.name} /> : <ImageIcon size={24} />}
  </button>;
}
