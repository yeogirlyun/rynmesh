import { createPortal } from "react-dom";
import { invoke } from "@tauri-apps/api/core";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Check, ChevronDown, Copy, Download, ExternalLink, FileText, FolderOpen, Image as ImageIcon, File as FileIcon, RefreshCw, Sheet, TriangleAlert } from "lucide-react";
import { AgentAPIError, agentReadBackoff, isTransientAgentReadError, agentFileUrl, agentTaskState, isAgentActive, rynAgent, type AgentArtifact, type AgentArtifactListing, type AgentFileArtifact, type AgentTask } from "../domain/rynAgent";
import { isTauriDesktop } from "../domain/nodeUrl";
import { NasPreview } from "../personal/NasPreview";
import { decodeAgentFileText, isOfficePreview, readAgentFilePreview } from "../domain/agentFilePreview";
import { AgentFileContext, fileAvailable } from "./RynAgentFileContext";
import "../personal/nas.css";

// Tool results that can add, change or remove a task's workspace files or their render evidence.
const FILE_TOOLS = new Set(["ryn_file_write", "ryn_file_edit", "ryn_file_manage", "ryn_apply_patch", "ryn_file_read", "ryn_render_inspect", "ryn_shell_execute", "ryn_process_read", "ryn_process_stop"]);
const EMPTY_FILES: AgentFileArtifact[] = [];
const HELPER_SCRIPT_RE = /\.(?:py|pyc|pyo|json|jsonl|sh|bash|zsh|ps1|bat|cmd|js|mjs|cjs|ts|tsx|yaml|yml|toml|ini|cfg|log|tmp|bak)$/i;
const INTERMEDIATE_RENDER_RE = /(?:^|[/_-])(?:page|slide|render|preview|crop|tmp|temp|check)[-_]?\d*\.(?:png|jpe?g|webp|ppm|pgm|pbm)$/i;

function deduplicateByName(items: AgentFileArtifact[]): AgentFileArtifact[] {
  const byName = new Map<string, AgentFileArtifact>();
  for (const item of items) {
    const key = item.name.trim().toLowerCase();
    const existing = byName.get(key);
    if (!existing || (item.status === "current" && existing.status !== "current") || item.revisions >= existing.revisions) {
      byName.set(key, item);
    }
  }
  return [...byName.values()];
}

function fileTurnIndex(file: AgentFileArtifact, maxTurnIndex: number): number {
  if (typeof file.turn_index === "number" && Number.isFinite(file.turn_index)) {
    return Math.max(0, Math.min(file.turn_index, maxTurnIndex));
  }
  if (file.goal_revisions?.length) {
    return Math.max(0, Math.min(file.goal_revisions[file.goal_revisions.length - 1] - 1, maxTurnIndex));
  }
  return 0;
}

const TaskTurnDeliverablesContext = createContext<{
  task: AgentTask;
  maxTurnIndex: number;
  deliverablesByTurn: Map<number, AgentFileArtifact[]>;
  openFile: (file: AgentFileArtifact) => void;
} | null>(null);

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
  const { t, i18n } = useTranslation("rynAgent");
  const zh = i18n.language.startsWith("zh");
  const [saveError, setSaveError] = useState(false);
  const [copied, setCopied] = useState(false);
  const menu = useRef<HTMLDetailsElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0, maxHeight: 320 });
  const positionMenu = useCallback(() => {
    const anchor = menu.current?.getBoundingClientRect();
    if (!anchor) return;
    let bottom = window.innerHeight - 8;
    let top = 8;
    for (let parent = menu.current?.parentElement; parent; parent = parent.parentElement) {
      if (/(auto|scroll|hidden)/.test(getComputedStyle(parent).overflowY)) {
        const rect = parent.getBoundingClientRect();
        bottom = Math.min(bottom, rect.bottom - 8);
        top = Math.max(top, rect.top + 8);
      }
    }
    const below = Math.max(0, bottom - anchor.bottom - 6);
    const above = Math.max(0, anchor.top - top - 6);
    const height = panel.current?.scrollHeight || 300;
    const up = below < height && above > below;
    const maxHeight = Math.max(0, up ? above : below);
    setMenuPosition({ top: up ? anchor.top - Math.min(height, maxHeight) - 6 : anchor.bottom + 6,
      left: Math.max(8, Math.min(anchor.right - 230, window.innerWidth - 238)), maxHeight });
  }, []);
  useEffect(() => {
    if (!menuOpen) return;
    positionMenu();
    window.addEventListener("resize", positionMenu);
    window.addEventListener("scroll", positionMenu, true);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(positionMenu);
    if (panel.current) observer?.observe(panel.current);
    return () => { observer?.disconnect(); window.removeEventListener("resize", positionMenu); window.removeEventListener("scroll", positionMenu, true); };
  }, [menuOpen, positionMenu]);
  const openable = fileAvailable(file);
  const onDisk = file.status !== "missing";
  const extension = file.name.split(".").pop()?.toLowerCase();
  const format = /^(xlsx?|csv)$/.test(extension || "") ? "Excel" : extension === "docx" ? "Word" : extension === "pptx" ? "PowerPoint" : extension?.toUpperCase();
  const Icon = format === "Excel" ? Sheet : file.preview === "image" ? ImageIcon : file.preview === "text" || format === "Word" ? FileText : FileIcon;
  const path = file.absolute_path || file.path;
  useEffect(() => {
    const close = (event: Event) => { if (event.type === "keydown" ? (event as KeyboardEvent).key === "Escape" : !menu.current?.contains(event.target as Node) && !panel.current?.contains(event.target as Node)) menu.current?.removeAttribute("open"); };
    document.addEventListener("pointerdown", close); document.addEventListener("keydown", close);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", close); };
  }, []);
  async function action(run: () => Promise<unknown>) {
    menu.current?.removeAttribute("open"); setSaveError(false);
    try { await run(); } catch { setSaveError(true); }
  }
  return <div className="ra-file-card" draggable={isTauriDesktop() && onDisk && Boolean(file.absolute_path)}
    onDragStart={event => {
      event.preventDefault();
      if ((event.target as HTMLElement).closest(".ra-file-actions")) return;
      if (file.absolute_path) void action(() => invoke("drag_local_file", { path: file.absolute_path }));
    }} data-role={file.role} data-status={file.status}>
    <span className="ra-file-icon" data-format={format}><Icon size={20} strokeWidth={1.7} /></span>
    <div className="ra-file-meta">
      <button className="ra-file-name" title={path} aria-label={t("openArtifact", { name: file.name })} disabled={!openable} onClick={() => onOpen(file)}>{file.name}</button>
      <small className="ra-file-sub">
        {openable && <span className="ra-file-hover-hint" aria-hidden="true">{zh ? "打开预览 · " : "Open preview · "}</span>}
        <span>{format}{file.bytes ? ` · ${size(file.bytes)}` : ""}{file.revisions > 1 ? ` · ${t("fileVersions", { count: file.revisions })}` : ""}</span>
      </small>
      {file.status !== "current" ? <small className="ra-file-warning"><TriangleAlert size={13} />{t(`fileStatus_${file.status}`)}</small>
        : null}
      {saveError && <small role="alert" className="ra-file-warning">{t("artifactReadError")}</small>}
      {copied && <small role="status">{t("filePathCopied")}</small>}
    </div>
    <div className="ra-file-actions">
      <details className="ra-file-open-menu" ref={menu} onToggle={event => {
        setMenuOpen(event.currentTarget.open);
        if (event.currentTarget.open) positionMenu();
      }}>
        <summary aria-label={t("openWaysNamed", { name: file.name })}>{t("openWays")}<ChevronDown size={13} /></summary>
        {menuOpen && createPortal(<div ref={panel} className="ra-file-open-options ra-file-open-floating" style={menuPosition}>
          <button disabled={!openable} onClick={() => { menu.current?.removeAttribute("open"); onOpen(file); }}><FileText size={15} />{t("previewFile")}</button>
          {isTauriDesktop() ? <><button disabled={!onDisk} onClick={() => void action(() => rynAgent.openFile(task.id, file.id, "open"))}><ExternalLink size={15} />{t("openDefaultApp")}</button>
            <button disabled={!onDisk} onClick={() => void action(() => rynAgent.openFile(task.id, file.id, "reveal"))}><FolderOpen size={15} />{t("revealFile")}</button></>
            : openable && <a href={agentFileUrl(task.id, file)} target="_blank" rel="noopener noreferrer" aria-label={t("openInNewTabNamed", { name: file.name })}><ExternalLink size={15} />{t("openInNewTab")}</a>}
          <button disabled={!openable} aria-label={t("downloadNamed", { name: file.name })} onClick={() => void action(() => saveCopy(task, file))}><Download size={15} />{t("downloadFile")}</button>
          <button onClick={() => void action(async () => { await navigator.clipboard.writeText(path); setCopied(true); })}><Copy size={15} />{t("copyFilePath")}</button>
          <details className="ra-file-info"><summary>{t("fileDetails")}</summary><span>{path}</span><span>{file.bytes !== undefined ? size(file.bytes) : ""}</span>
            {file.preview === "image" && <span>{t(file.render_inspected ? "renderChecked" : "renderNotChecked")}</span>}</details>
        </div>, document.body)}
      </details>
    </div>
  </div>;
}

export function TurnDeliverables({ turnIndex }: { turnIndex: number }) {
  const ctx = useContext(TaskTurnDeliverablesContext);
  const { t } = useTranslation("rynAgent");
  if (!ctx || turnIndex >= ctx.maxTurnIndex) return null;
  const turnFiles = ctx.deliverablesByTurn.get(turnIndex);
  if (!turnFiles?.length) return null;
  const available = turnFiles.filter(f => f.status !== "missing").length;
  return <section className="ra-files ra-turn-files" aria-label={t("resultFiles")}>
    {!available && <div className="ra-files-head"><span role="status" aria-live="polite"><TriangleAlert size={15} />{t("filesUnavailable", { count: 0 })}</span></div>}
    {turnFiles.map(item => <FileCard key={item.id} task={ctx.task} file={item} onOpen={ctx.openFile} />)}
  </section>;
}

export function TaskArtifacts({ task, maxTurnIndex = 0, children }: { task: AgentTask; maxTurnIndex?: number; children?: ReactNode }) {
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
    let failures = 0; let unchanged = 0; let loading = false;
    let previous = "";
    const load = async () => {
      if (controller.signal.aborted || loading) return;
      if (timer) clearTimeout(timer);
      loading = true;
      try {
        const value = await rynAgent.artifacts(task.id, controller.signal);
        if (controller.signal.aborted) return;
        // Polling an unchanged file listing must not invalidate every Markdown
        // message while the user types in the composer.
        const snapshot = JSON.stringify(value);
        unchanged = snapshot === previous ? unchanged + 1 : 0;
        previous = snapshot;
        setListing(current => JSON.stringify(current) === snapshot ? current : value); setError(false);
        const unavailable = value.files?.some(file => file.status === "unavailable");
        failures = unavailable ? failures + 1 : 0;
        // A completed model turn may still have a command producing files.
        if (value.pending_files) timer = setTimeout(load, 1500);
        else if (unavailable) timer = setTimeout(load, Math.min(30000, 750 * 2 ** Math.min(failures - 1, 6)));
        // Tool/file revisions trigger an immediate refresh. While the model is thinking,
        // re-hashing unchanged files needs only a slow fallback; shell delivery stays polled.
        else if (!finished) timer = setTimeout(load, Math.min(30000, 5000 * 2 ** Math.min(unchanged, 3)));
      } catch (reason) {
        if (controller.signal.aborted) return;
        if (reason instanceof AgentAPIError && (reason.code === "agent_task_deleted" || reason.code === "agent_task_not_found" || reason.status === 404 || reason.status === 410)) return;
        setError(true);
        if (isTransientAgentReadError(reason)) {
          timer = setTimeout(load, agentReadBackoff(++failures));
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
      if (reason instanceof AgentAPIError && reason.code === "agent_artifact_content_changed" && !signal.aborted) {
        setReload(n => n + 1);
        if (/\.pdf$/i.test(file.name)) throw reason;
      }
      throw new Error(t("artifactReadError"));
    });
  // The version, not the listing object's identity, decides when bytes are fetched again.
  }, [task.id, file?.id, file?.sha256, file?.media_type, file?.name, t]);
  const readFileText = useCallback(async (signal: AbortSignal) => decodeAgentFileText(await loadFile(signal)), [loadFile]);
  const readDocument = useCallback(async (signal: AbortSignal) => {
    if (!file) throw new Error(t("artifactReadError"));
    try { return await readAgentFilePreview(task.id, file, signal); }
    catch (reason) {
      if (reason instanceof AgentAPIError && reason.code === "agent_artifact_content_changed" && !signal.aborted) setReload(n => n + 1);
      throw reason;
    }
  }, [task.id, file?.id, file?.sha256, t]);
  const items = listing?.artifacts || [];
  const resultFiles = files.filter(item => item.source !== "computer_observation" && !item.path.replaceAll("\\", "/").startsWith(".ryn/computer/") && !item.path.replaceAll("\\", "/").startsWith(".ryn/attachments/"));
  const deliverablesByTurn = useMemo(() => {
    const map = new Map<number, AgentFileArtifact[]>();
    for (const item of resultFiles) {
      if (item.role !== "deliverable") continue;
      const tIdx = fileTurnIndex(item, maxTurnIndex);
      const list = map.get(tIdx) || [];
      list.push(item);
      map.set(tIdx, list);
    }
    for (const [tIdx, list] of map.entries()) {
      map.set(tIdx, deduplicateByName(list));
    }
    return map;
  }, [resultFiles, maxTurnIndex]);
  const turnContext = useMemo(() => ({ task, maxTurnIndex, deliverablesByTurn, openFile }), [task, maxTurnIndex, deliverablesByTurn, openFile]);
  const currentTurnFiles = resultFiles.filter(item => fileTurnIndex(item, maxTurnIndex) === maxTurnIndex);
  const deliverables = useMemo(() => deduplicateByName(currentTurnFiles.filter(item => item.role === "deliverable")), [currentTurnFiles]);
  const deliverableNames = useMemo(() => new Set(deliverables.map(item => item.name.trim().toLowerCase())), [deliverables]);
  const supporting = useMemo(() => deduplicateByName(
    currentTurnFiles.filter(item =>
      item.role !== "deliverable" &&
      item.source !== "command" &&
      !HELPER_SCRIPT_RE.test(item.name) &&
      !INTERMEDIATE_RENDER_RE.test(item.path.replaceAll("\\", "/")) &&
      !deliverableNames.has(item.name.trim().toLowerCase()),
    ),
  ), [currentTurnFiles, deliverableNames]);
  // A file on disk is still work in progress until the turn and its background
  // file producers have settled. Keep the listing available for process previews.
  const showCurrentFiles = finished && !listing?.pending_files;
  const showResultFiles = showCurrentFiles && (deliverables.length > 0 || supporting.length > 0);
  const partial = ["failed", "cancelled", "interrupted", "paused"].includes(agentTaskState(task));
  const availableCount = deliverables.filter(file => file.status !== "missing").length || supporting.filter(file => file.status !== "missing").length;
  useEffect(() => {
    if (!chosenFile && !chosen) return;
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") { setChosenFile(""); setChosen(null); } };
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, [chosenFile, chosen]);
  return <AgentFileContext.Provider value={fileContext}><TaskTurnDeliverablesContext.Provider value={turnContext}>
    {children}
    {showResultFiles && <section className="ra-files" aria-label={t(finished ? "resultFiles" : "filesSoFar")}>
      {error && items.length === 0 && <p role="alert">{t("artifactListError")} <button onClick={() => setReload(n => n + 1)}>{t("retry")}</button></p>}
      {(!availableCount || partial) && <div className="ra-files-head"><span role="status" aria-live="polite">{availableCount ? <Check size={15} /> : <TriangleAlert size={15} />}{t(!availableCount ? "filesUnavailable" : "filesSavedPartial", { count: availableCount })}</span>
        <button className="ra-icon" aria-label={t("refreshFiles")} title={t("refreshFiles")} onClick={() => setReload(n => n + 1)}><RefreshCw size={13} /></button></div>}
      {deliverables.map(item => <FileCard key={item.id} task={task} file={item} onOpen={openFile} />)}
      {supporting.length > 0 && <details className={`ra-files-supporting${deliverables.length > 0 ? " ra-sr-only" : ""}`}><summary>{t("supportingFiles", { count: supporting.length })}</summary>
        {supporting.map(item => <FileCard key={item.id} task={task} file={item} onOpen={openFile} />)}</details>}
    </section>}
      {file && <div className="ra-artifact-preview"><NasPreview key={`${task.id}/${file.id}/${file.sha256}`} source={{ id: task.id, name: t("taskFilesSource"), allow_ai: false }}
        entry={{ name: file.name, path: file.path, directory: false, size: file.bytes || 0, modified: file.updated_at || "" }}
        onClose={() => setChosenFile("")} download={() => { void saveCopy(task, file).catch(() => setError(true)); }} askAi={() => {}} busy={false}
        loadImage={file.preview === "image" ? loadFile : undefined} readText={file.preview === "text" ? readFileText : undefined}
        loadPdf={/\.pdf$/i.test(file.name) ? loadFile : undefined}
        loadDocument={isOfficePreview(file.name) ? readDocument : undefined} /></div>}
    {showCurrentFiles && (items.length > 0 || (error && !showResultFiles)) && <section className="ra-files ra-artifacts" aria-label={t("artifacts")}>
      {error && <p role="alert">{t("artifactListError")} <button onClick={() => setReload(n => n + 1)}>{t("retry")}</button></p>}
      {items.length > 0 && partial && <div className="ra-files-head" role="status"><Check size={15} />{t("filesSavedPartial", { count: items.length })}</div>}
      {items.map(item => <div className="ra-file-card" key={item.id}><span className="ra-file-icon"><FileText size={20} strokeWidth={1.7} /></span>
        <div className="ra-file-meta"><button className="ra-file-name" aria-label={t("openArtifact", { name: item.name })} onClick={() => setChosen(item)}>{item.name}</button><small>{item.name.split(".").pop()?.toUpperCase()}</small></div>
        <button onClick={() => setChosen(item)}>{t("previewFile")}</button></div>)}
      {chosen && <div className="ra-artifact-preview"><NasPreview key={`${task.id}/${task.control_revision}/${task.scope_revision}/${chosen.id}`} source={{ id: task.id, name: t("artifacts"), allow_ai: false }}
        entry={{ name: chosen.name, path: chosen.name, directory: false, size: chosen.bytes, modified: chosen.created_at || "" }}
        onClose={() => setChosen(null)} download={() => {}} askAi={() => {}} busy={false} readText={readText} readOnly /></div>}
    </section>}
  </TaskTurnDeliverablesContext.Provider></AgentFileContext.Provider>;
}
