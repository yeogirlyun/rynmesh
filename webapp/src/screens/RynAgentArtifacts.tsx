import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Check, ChevronDown, Copy, Download, ExternalLink, FileText, FolderOpen, Image as ImageIcon, File as FileIcon, RefreshCw, Sheet, TriangleAlert } from "lucide-react";
import { AgentAPIError, agentFileUrl, agentTaskState, isAgentActive, rynAgent, type AgentArtifact, type AgentArtifactListing, type AgentFileArtifact, type AgentTask } from "../domain/rynAgent";
import { isTauriDesktop } from "../domain/nodeUrl";
import { NasPreview } from "../personal/NasPreview";
import { AgentFileContext, fileAvailable } from "./RynAgentFileContext";
import "../personal/nas.css";

// Tool results that can add, change or remove a task's workspace files or their render evidence.
const FILE_TOOLS = new Set(["ryn_file_write", "ryn_file_edit", "ryn_file_manage", "ryn_apply_patch", "ryn_file_read", "ryn_render_inspect", "ryn_shell_execute", "ryn_process_read", "ryn_process_stop"]);
const EMPTY_FILES: AgentFileArtifact[] = [];

function size(bytes = 0): string {
  return bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1048576).toFixed(1)} MiB`;
}

async function saveCopy(task: AgentTask, file: AgentFileArtifact) {
  const blob = await rynAgent.fileContent(task.id, file);
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url; link.download = file.name; link.rel = "noopener";
  document.body.append(link); link.click(); link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 60000);
}

function FileCard({ task, file, onOpen }: { task: AgentTask; file: AgentFileArtifact; onOpen: (file: AgentFileArtifact) => void }) {
  const { t } = useTranslation("rynAgent");
  const [saveError, setSaveError] = useState(false);
  const [copied, setCopied] = useState(false);
  const menu = useRef<HTMLDetailsElement>(null);
  const openable = fileAvailable(file);
  const onDisk = file.status !== "missing";
  const extension = file.name.split(".").pop()?.toLowerCase();
  const format = /^(xlsx?|csv)$/.test(extension || "") ? "Excel" : extension === "docx" ? "Word" : extension === "pptx" ? "PowerPoint" : extension?.toUpperCase();
  const Icon = format === "Excel" ? Sheet : file.preview === "image" ? ImageIcon : file.preview === "text" || format === "Word" ? FileText : FileIcon;
  const path = file.absolute_path || file.path;
  useEffect(() => {
    const close = (event: Event) => { if (event.type === "keydown" ? (event as KeyboardEvent).key === "Escape" : !menu.current?.contains(event.target as Node)) menu.current?.removeAttribute("open"); };
    document.addEventListener("pointerdown", close); document.addEventListener("keydown", close);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", close); };
  }, []);
  async function action(run: () => Promise<unknown>) {
    menu.current?.removeAttribute("open"); setSaveError(false);
    try { await run(); } catch { setSaveError(true); }
  }
  return <div className="ra-file-card" data-role={file.role} data-status={file.status}>
    <span className="ra-file-icon" data-format={format}><Icon size={22} strokeWidth={1.6} /></span>
    <div className="ra-file-meta">
      <button className="ra-file-name" title={path} aria-label={t("openArtifact", { name: file.name })} disabled={!openable} onClick={() => onOpen(file)}>{file.name}</button>
      <small>{format}{file.revisions > 1 ? ` · ${t("fileVersions", { count: file.revisions })}` : ""}</small>
      {file.attribution === "observed_during_command" && <small>{t("fileCommandObservation")}</small>}
      {file.status !== "current" ? <small className="ra-file-warning"><TriangleAlert size={13} />{t(`fileStatus_${file.status}`)}</small>
        : null}
      {saveError && <small role="alert" className="ra-file-warning">{t("artifactReadError")}</small>}
      {copied && <small role="status">{t("filePathCopied")}</small>}
    </div>
    <div className="ra-file-actions">
      <details className="ra-file-open-menu" ref={menu} onToggle={event => {
        const element = event.currentTarget;
        element.dataset.placement = window.innerHeight - element.getBoundingClientRect().bottom < 240 ? "up" : "down";
      }}>
        <summary aria-label={t("openWaysNamed", { name: file.name })}>{t("openWays")}<ChevronDown size={14} /></summary>
        <div className="ra-file-open-options">
          <button disabled={!openable} onClick={() => { menu.current?.removeAttribute("open"); onOpen(file); }}><FileText size={15} />{t("previewFile")}</button>
          {isTauriDesktop() ? <><button disabled={!onDisk} onClick={() => void action(() => rynAgent.openFile(task.id, file.id, "open"))}><ExternalLink size={15} />{t("openDefaultApp")}</button>
            <button disabled={!onDisk} onClick={() => void action(() => rynAgent.openFile(task.id, file.id, "reveal"))}><FolderOpen size={15} />{t("revealFile")}</button></>
            : openable && <a href={agentFileUrl(task.id, file)} target="_blank" rel="noopener noreferrer" aria-label={t("openInNewTabNamed", { name: file.name })}><ExternalLink size={15} />{t("openInNewTab")}</a>}
          <button disabled={!openable} aria-label={t("downloadNamed", { name: file.name })} onClick={() => void action(() => saveCopy(task, file))}><Download size={15} />{t("downloadFile")}</button>
          <button onClick={() => void action(async () => { await navigator.clipboard.writeText(path); setCopied(true); })}><Copy size={15} />{t("copyFilePath")}</button>
          <details className="ra-file-info"><summary>{t("fileDetails")}</summary><span>{path}</span><span>{file.bytes !== undefined ? size(file.bytes) : ""}</span>
            {file.preview === "image" && <span>{t(file.render_inspected ? "renderChecked" : "renderNotChecked")}</span>}</details>
        </div>
      </details>
    </div>
  </div>;
}

export function TaskArtifacts({ task, children }: { task: AgentTask; children?: ReactNode }) {
  const { t } = useTranslation("rynAgent");
  const [listing, setListing] = useState<AgentArtifactListing | null>(null);
  const [chosen, setChosen] = useState<AgentArtifact | null>(null);
  const [chosenFile, setChosenFile] = useState("");
  const [error, setError] = useState(false);
  const [reload, setReload] = useState(0);
  const writes = task.events.filter(e => e.type === "tool_result" && e.tool === "ryn_create_text_file").length;
  const fileResults = task.events.filter(e => e.type === "tool_result" && e.tool && FILE_TOOLS.has(e.tool)).at(-1)?.seq;
  const finished = !isAgentActive(agentTaskState(task));
  useEffect(() => { setListing(null); setChosenFile(""); }, [task.id]);
  useEffect(() => {
    // A changed scope or control revision closes resource previews; workspace files stay listed
    // until the fresh listing arrives, so an open file follows its newest version.
    const controller = new AbortController(); setChosen(null); setError(false);
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0; let loading = false;
    const load = async () => {
      if (controller.signal.aborted || loading) return;
      if (timer) clearTimeout(timer);
      loading = true;
      try {
        const value = await rynAgent.artifacts(task.id, controller.signal);
        if (controller.signal.aborted) return;
        // Polling an unchanged file listing must not invalidate every Markdown
        // message while the user types in the composer.
        setListing(current => JSON.stringify(current) === JSON.stringify(value) ? current : value); setError(false);
        const unavailable = value.files?.some(file => file.status === "unavailable");
        failures = unavailable ? failures + 1 : 0;
        // A completed model turn may still have a command producing files.
        if (!finished || value.pending_files) timer = setTimeout(load, 750);
        else if (unavailable) timer = setTimeout(load, Math.min(30000, 750 * 2 ** Math.min(failures - 1, 6)));
      } catch (reason) {
        if (controller.signal.aborted) return;
        setError(true); setListing(current => current && { ...current, artifacts: [] });
        if (!(reason instanceof AgentAPIError) || reason.status >= 500 || reason.status === 409) {
          timer = setTimeout(load, Math.min(30000, 250 * 2 ** Math.min(failures++, 7)));
        }
      } finally { loading = false; }
    };
    const onVisible = () => { if (document.visibilityState === "visible") void load(); };
    window.addEventListener("focus", load); document.addEventListener("visibilitychange", onVisible);
    void load();
    return () => { controller.abort(); if (timer) clearTimeout(timer); window.removeEventListener("focus", load); document.removeEventListener("visibilitychange", onVisible); };
  }, [task.id, task.control_revision, task.scope_revision, task.file_artifact_revision, writes, fileResults, task.result, finished, reload]);
  const readText = useCallback(async (signal: AbortSignal) => {
    if (!chosen) throw new Error(t("artifactReadError"));
    try { return (await rynAgent.artifact(task.id, chosen.id, signal)).text; }
    catch { throw new Error(t("artifactReadError")); }
  }, [task.id, chosen, t]);
  const files = listing?.files || EMPTY_FILES;
  const file = files.find(item => item.id === chosenFile && fileAvailable(item));
  const openFile = useCallback((next: AgentFileArtifact) => setChosenFile(next.id), []);
  const fileContext = useMemo(() => ({ taskID: task.id, files, open: openFile }), [task.id, files, openFile]);
  const loadFile = useCallback((signal: AbortSignal) => {
    if (!file) return Promise.reject(new Error(t("artifactReadError")));
    return rynAgent.fileContent(task.id, file, signal).catch(reason => {
      if (reason instanceof AgentAPIError && reason.code === "agent_artifact_content_changed" && !signal.aborted) setReload(n => n + 1);
      throw new Error(t("artifactReadError"));
    });
  // The version, not the listing object's identity, decides when bytes are fetched again.
  }, [task.id, file?.id, file?.sha256, file?.media_type, t]);
  const readFileText = useCallback(async (signal: AbortSignal) => (await loadFile(signal)).text(), [loadFile]);
  const items = listing?.artifacts || [];
  const deliverables = files.filter(item => item.role === "deliverable");
  const supporting = files.filter(item => item.role !== "deliverable");
  const unbacked = listing?.unbacked_mentions || [];
  const partial = ["failed", "cancelled", "interrupted", "paused"].includes(agentTaskState(task));
  const availableCount = deliverables.filter(file => file.status !== "missing").length || files.filter(file => file.status !== "missing").length;
  useEffect(() => {
    if (!chosenFile && !chosen) return;
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") { setChosenFile(""); setChosen(null); } };
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, [chosenFile, chosen]);
  return <AgentFileContext.Provider value={fileContext}>
    {children}
    {files.length > 0 && <section className="ra-files" aria-label={t(finished ? "resultFiles" : "filesSoFar")}>
      <div className="ra-files-head"><span role="status" aria-live="polite">{availableCount ? <Check size={15} /> : <TriangleAlert size={15} />}{t(!availableCount ? "filesUnavailable" : partial ? "filesSavedPartial" : "filesReady", { count: availableCount })}</span>
        <button className="ra-icon" aria-label={t("refreshFiles")} title={t("refreshFiles")} onClick={() => setReload(n => n + 1)}><RefreshCw size={13} /></button></div>
      {deliverables.map(item => <FileCard key={item.id} task={task} file={item} onOpen={openFile} />)}
      {supporting.length > 0 && <details className="ra-files-supporting"><summary>{t("supportingFiles", { count: supporting.length })}</summary>
        {supporting.map(item => <FileCard key={item.id} task={task} file={item} onOpen={openFile} />)}</details>}
      {finished && unbacked.map(name => <p key={name} className="ra-file-warning" role="note"><TriangleAlert size={13} />{t("unbackedMention", { name })}</p>)}
      {file && <div className="ra-artifact-preview"><NasPreview key={`${task.id}/${file.id}/${file.sha256}`} source={{ id: task.id, name: t("taskFilesSource"), allow_ai: false }}
        entry={{ name: file.name, path: file.path, directory: false, size: file.bytes || 0, modified: file.updated_at || "" }}
        onClose={() => setChosenFile("")} download={() => { void saveCopy(task, file).catch(() => setError(true)); }} askAi={() => {}} busy={false}
        loadImage={file.preview === "image" ? loadFile : undefined} readText={file.preview === "text" ? readFileText : undefined} /></div>}
    </section>}
    {!files.length && finished && unbacked.map(name => <p key={name} className="ra-file-warning" role="note"><TriangleAlert size={13} />{t("unbackedMention", { name })}</p>)}
    {(items.length > 0 || error) && <section className="ra-files ra-artifacts" aria-label={t("artifacts")}>
      {error && <p role="alert">{t("artifactListError")} <button onClick={() => setReload(n => n + 1)}>{t("retry")}</button></p>}
      {items.length > 0 && <div className="ra-files-head" role="status"><Check size={15} />{t(partial ? "filesSavedPartial" : "filesReady", { count: items.length })}</div>}
      {items.map(item => <div className="ra-file-card" key={item.id}><span className="ra-file-icon"><FileText size={22} strokeWidth={1.6} /></span>
        <div className="ra-file-meta"><button className="ra-file-name" aria-label={t("openArtifact", { name: item.name })} onClick={() => setChosen(item)}>{item.name}</button><small>{item.name.split(".").pop()?.toUpperCase()}</small></div>
        <button onClick={() => setChosen(item)}>{t("previewFile")}</button></div>)}
      {chosen && <div className="ra-artifact-preview"><NasPreview key={`${task.id}/${task.control_revision}/${task.scope_revision}/${chosen.id}`} source={{ id: task.id, name: t("artifacts"), allow_ai: false }}
        entry={{ name: chosen.name, path: chosen.name, directory: false, size: chosen.bytes, modified: chosen.created_at || "" }}
        onClose={() => setChosen(null)} download={() => {}} askAi={() => {}} busy={false} readText={readText} readOnly /></div>}
    </section>}
  </AgentFileContext.Provider>;
}
