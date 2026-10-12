import { useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { ArrowDownToLine, ArrowLeft, ArrowUpDown, Check, CheckCheck, ChevronRight, Cloud, File, FileText, Folder, FolderPlus, HardDrive, Image, Info, LayoutGrid, List, LockKeyhole, Monitor, Pause, Play, Plus, Search, Settings2, ShieldCheck, Sparkles, Upload, X } from "lucide-react";
import { Modal } from "./components";
import { usePersonal } from "./model";
import "./drives.css";

type Provider = "nas" | "google" | "onedrive" | "baidu";
type Drive = { id: string; provider: Provider; name: string; account: string; used: number; total: number; state: "online" | "expired"; writable: boolean; ai: boolean };
type Entry = { id: string; name: string; kind: "folder" | "text" | "pdf" | "image" | "sheet"; size: string; date: string; parent: string };
type Transfer = { id: string; name: string; source: string; kind: "upload" | "download"; progress: number; state: "paused" | "running" | "done" };
const providerNames: Record<Provider, string> = { nas: "NAS", google: "Google Drive", onedrive: "OneDrive", baidu: "Baidu Netdisk" };
const seedEntries: Entry[] = [
  { id: "projects", name: "projects", kind: "folder", size: "—", date: "2026-10-08", parent: "" },
  { id: "photos", name: "photos", kind: "folder", size: "—", date: "2026-10-07", parent: "" },
  { id: "archive", name: "archive", kind: "folder", size: "—", date: "2026-10-06", parent: "" },
  { id: "readme", name: "workspace-notes.md", kind: "text", size: "4.2 KB", date: "2026-10-08", parent: "" },
  { id: "plan", name: "Autumn travel.pdf", kind: "pdf", size: "2.4 MB", date: "2026-10-07", parent: "" },
  { id: "budget", name: "October budget.xlsx", kind: "sheet", size: "128 KB", date: "2026-10-06", parent: "" },
  { id: "project-note", name: "Rynmesh-design.md", kind: "text", size: "8.6 KB", date: "2026-10-08", parent: "projects" },
  { id: "project-plan", name: "Milestones.pdf", kind: "pdf", size: "1.8 MB", date: "2026-10-06", parent: "projects" },
  { id: "photo-note", name: "Photo locations.md", kind: "text", size: "2.1 KB", date: "2026-10-07", parent: "photos" },
];

function Brand({ provider }: { provider: Provider }) {
  return <span className={`drive-brand ${provider}`} aria-hidden="true">
    {provider === "nas" ? <HardDrive size={23} strokeWidth={1.6} /> : provider === "google" ?
      <svg viewBox="0 0 32 30" width="26" height="25"><path d="M11 2h10l10 18H21Z" fill="#fbbc04" /><path d="M11 2 1 20l5 9 10-18Z" fill="#34a853" /><path d="M1 20h30l-5 9H6Z" fill="#4285f4" /></svg> :
      provider === "onedrive" ? <Cloud size={29} fill="currentColor" strokeWidth={1.2} /> : <Cloud size={27} strokeWidth={2.2} />}
  </span>;
}
function FileIcon({ kind, large = false }: { kind: Entry["kind"]; large?: boolean }) {
  const Icon = kind === "folder" ? Folder : kind === "image" ? Image : kind === "text" || kind === "pdf" ? FileText : File;
  return <span className={`drive-file-icon ${kind}${large ? " large" : ""}`}><Icon size={large ? 34 : 20} strokeWidth={1.6} fill={kind === "folder" ? "currentColor" : "none"} /></span>;
}

/** Review-only workspace: all changes are in memory; no provider or NAS calls. */
export function DrivePreview() {
  const { demo } = usePersonal();
  const { t } = useTranslation();
  if (!demo) return <div className="screen-stack"><h1>{t("drive.title")}</h1><p>{t("drive.previewOnly")}</p><Link className="pf-button" to="/drives?client=fixture" reloadDocument>{t("drive.openPreview")}</Link><Link to="/nas">{t("drive.existingNas")}</Link></div>;
  return <DriveWorkspace />;
}

function DriveWorkspace() {
  const { t } = useTranslation();
  const text = (key: string) => t(`drive.${key}`);
  const [drives, setDrives] = useState<Drive[]>(() => [
    { id: "home", provider: "nas", name: text("homeNas"), account: "Mac mini · SMB", used: 1240, total: 4000, state: "online", writable: true, ai: true },
    { id: "google", provider: "google", name: "Google Drive", account: "alex@example.com", used: 32.4, total: 100, state: "online", writable: true, ai: false },
    { id: "onedrive", provider: "onedrive", name: "OneDrive", account: "alex@work.example", used: 186, total: 1000, state: "online", writable: false, ai: false },
    { id: "baidu", provider: "baidu", name: text("baidu"), account: "Alex · " + text("personalAccount"), used: 680, total: 2000, state: "expired", writable: true, ai: false },
  ]);
  const [selected, setSelected] = useState("home");
  const [filter, setFilter] = useState("all");
  const [files, setFiles] = useState<Record<string, Entry[]>>({ home: seedEntries, google: seedEntries.filter(e => !["photos", "photo-note"].includes(e.id)), onedrive: seedEntries.filter(e => ["projects", "budget", "project-note"].includes(e.id)) });
  const [path, setPath] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [reverse, setReverse] = useState(false);
  const [view, setView] = useState<"list" | "grid">("list");
  const [picked, setPicked] = useState("readme");
  const [adding, setAdding] = useState(false);
  const [settings, setSettings] = useState(false);
  const [transfersOpen, setTransfersOpen] = useState(false);
  const [folderOpen, setFolderOpen] = useState(false);
  const [folderName, setFolderName] = useState("");
  const [folderError, setFolderError] = useState("");
  const [preview, setPreview] = useState<Entry | null>(null);
  const [aiOpen, setAiOpen] = useState(false);
  const [notice, setNotice] = useState("");
  const [transfers, setTransfers] = useState<Transfer[]>([{ id: "sample", name: "Design references.zip", source: text("homeNas"), kind: "download", progress: 64, state: "paused" }]);
  const upload = useRef<HTMLInputElement>(null);
  const drive = drives.find(d => d.id === selected)!;
  const available = drive.state === "online";
  const allFiles = files[selected] || [];
  const entryName = (e: Entry) => ["projects", "photos", "archive"].includes(e.name) ? text(e.name) : e.name;
  const entries = allFiles.filter(e => e.parent === (path.at(-1) || "") && entryName(e).toLowerCase().includes(query.toLowerCase())).sort((a, b) => Number(b.kind === "folder") - Number(a.kind === "folder") || entryName(a).localeCompare(entryName(b)) * (reverse ? -1 : 1));
  const entry = entries.find(e => e.id === picked);
  const visibleDrives = drives.filter(d => filter === "all" || (filter === "local" ? d.provider === "nas" : d.provider !== "nas"));
  const activeTransfers = transfers.filter(tr => tr.state !== "done").length;
  function choose(d: Drive) { setSelected(d.id); setPath([]); setQuery(""); setPicked(d.id === "home" ? "readme" : ""); }
  function enter(e: Entry) { if (e.kind === "folder") { setPath([...path, e.id]); setQuery(""); setPicked(""); } else setPreview(e); }
  function transfer(name: string, kind: Transfer["kind"]) { setTransfers(current => [{ id: crypto.randomUUID(), name, source: drive.name, kind, progress: 0, state: "paused" }, ...current]); setNotice(text("taskAdded")); setTransfersOpen(true); }
  function createFolder(event: FormEvent) {
    event.preventDefault(); const name = folderName.trim();
    if (!name || /[\\/:*?"<>|]/.test(name)) { setFolderError(text("invalidFolder")); return; }
    if (allFiles.some(e => e.parent === (path.at(-1) || "") && entryName(e) === name)) { setFolderError(text("duplicateFolder")); return; }
    setFiles({ ...files, [selected]: [...allFiles, { id: crypto.randomUUID(), name, kind: "folder", size: "—", date: "2026-10-08", parent: path.at(-1) || "" }] });
    setFolderOpen(false); setFolderName(""); setNotice(text("folderCreated"));
  }
  return <div className="drive-page">
    <header className="drive-heading"><div><div className="drive-eyebrow">{text("eyebrow")}</div><h1>{text("title")}<span className="drive-beta">{text("preview")}</span></h1><p>{text("subtitle")}</p></div><div className="drive-actions"><button className="pf-button" onClick={() => setTransfersOpen(true)}><ArrowDownToLine size={16} />{text("transfers")}{activeTransfers > 0 && <span className="drive-count">{activeTransfers}</span>}</button><button className="pf-button primary" onClick={() => setAdding(true)}><Plus size={17} />{text("add")}</button></div></header>
    <div className="drive-preview-note"><Info size={14} /><span>{text("previewNote")}</span><span className="drive-preview-version">{text("previewVersion")}</span></div>
    <section aria-label={text("sources")}>
      <div className="drive-section-heading"><div className="drive-tabs" aria-label={text("filterSources")}>{["all", "local", "cloud"].map(key => <button key={key} aria-pressed={filter === key} className={filter === key ? "active" : ""} onClick={() => { setFilter(key); const matches = drives.filter(d => key === "all" || (key === "local" ? d.provider === "nas" : d.provider !== "nas")); if (matches.length && !matches.some(d => d.id === selected)) choose(matches[0]); }}>{text(key)}<span>{drives.filter(d => key === "all" || (key === "local" ? d.provider === "nas" : d.provider !== "nas")).length}</span></button>)}</div><span className="drive-source-note"><ShieldCheck size={14} />{text("stayInPlace")}</span></div>
      <div className="drive-sources">{visibleDrives.map(d => <button key={d.id} onClick={() => choose(d)} aria-pressed={d.id === selected} className={`drive-source${d.id === selected ? " selected" : ""}`}><div className="drive-source-top"><Brand provider={d.provider} /><span className={`drive-status ${d.state}`}><i />{text(d.state === "online" ? "connected" : "reauthNeeded")}</span></div><strong>{d.name}</strong><span className="drive-account">{d.account}</span><div className={`drive-capacity ${d.state === "expired" ? "muted" : ""}`}><span style={{ width: `${d.used / d.total * 100}%` }} /></div><div className="drive-source-bottom"><span>{d.used >= 1000 ? `${(d.used / 1000).toFixed(2)} TB` : `${d.used} GB`} / {d.total >= 1000 ? `${d.total / 1000} TB` : `${d.total} GB`}</span><span>{text(d.writable ? "readWrite" : "readOnly")}</span></div></button>)}{visibleDrives.length === 0 && <div className="drive-empty"><Cloud size={30} /><p>{text("noSources")}</p><button className="pf-button" onClick={() => setAdding(true)}>{text("add")}</button></div>}</div>
    </section>
    <section className="drive-workspace" aria-label={text("files")}>
      <div className="drive-workspace-top"><div className="drive-workspace-source"><Brand provider={drive.provider} /><div><h2>{drive.name}</h2><p><span className={`drive-dot ${drive.state}`} />{drive.provider === "nas" ? t("drive.viaDevice", { device: drive.account.split(" · ")[0] }) : text("viaCloud")}</p></div></div><button className="pf-button drive-settings" onClick={() => setSettings(true)}><Settings2 size={15} />{text("connectionSettings")}</button></div>
      <div className="drive-toolbar"><nav className="drive-breadcrumb" aria-label={text("folderPath")}><button onClick={() => { setPath([]); setQuery(""); setPicked(""); }}><Folder size={15} />{text("allFiles")}</button>{path.map((id, index) => <span key={id}><ChevronRight size={13} /><button onClick={() => { setPath(path.slice(0, index + 1)); setQuery(""); setPicked(""); }}>{entryName(allFiles.find(e => e.id === id)!)}</button></span>)}</nav><div className="drive-actions"><button className="pf-button" disabled={!available || !drive.writable} onClick={() => { setFolderError(""); setFolderOpen(true); }}><FolderPlus size={15} />{text("newFolder")}</button><button className="pf-button primary" disabled={!available || !drive.writable} onClick={() => upload.current?.click()}><Upload size={15} />{text("upload")}</button><input ref={upload} type="file" hidden multiple onChange={event => { const selectedFiles = Array.from(event.target.files || []); if (selectedFiles.length) { setTransfers(current => [...selectedFiles.map(f => ({ id: crypto.randomUUID(), name: f.name, source: drive.name, kind: "upload" as const, progress: 0, state: "paused" as const })), ...current]); setNotice(text("taskAdded")); setTransfersOpen(true); } event.target.value = ""; }} /></div></div>
      {!available ? <div className="drive-unavailable"><span className="drive-empty-symbol"><LockKeyhole size={29} /></span><h3>{text("expiredTitle")}</h3><p>{text("expiredBody")}</p><button className="pf-button primary" onClick={() => setSettings(true)}>{text("reauthorize")}</button></div> : <>
        {!drive.writable && <div className="drive-readonly"><LockKeyhole size={13} />{text("readOnlyNote")}</div>}
        <div className="drive-browser"><div className="drive-file-area"><div className="drive-list-tools"><label className="drive-search"><Search size={16} /><input aria-label={text("search")} placeholder={text("search")} value={query} onChange={e => setQuery(e.target.value)} />{query && <button aria-label={text("clearSearch")} onClick={() => setQuery("")}><X size={14} /></button>}</label><div className="drive-view-switch"><button aria-label={text("listView")} aria-pressed={view === "list"} className={view === "list" ? "active" : ""} onClick={() => setView("list")}><List size={17} /></button><button aria-label={text("gridView")} aria-pressed={view === "grid"} className={view === "grid" ? "active" : ""} onClick={() => setView("grid")}><LayoutGrid size={16} /></button></div></div>
          {entries.length === 0 ? <div className="drive-empty"><Search size={28} /><h3>{text(query ? "noMatches" : "emptyFolder")}</h3><p>{text(query ? "trySearch" : drive.writable ? "emptyFolderBody" : "emptyReadOnlyBody")}</p>{query && <button className="pf-button" onClick={() => setQuery("")}>{text("clearSearch")}</button>}</div> : view === "list" ? <div className="drive-table-scroll"><table className="drive-table" aria-label={text("files")}><thead><tr><th><button onClick={() => setReverse(!reverse)}>{text("name")}<ArrowUpDown size={12} /></button></th><th>{text("modified")}</th><th>{text("size")}</th><th><span className="drive-sr-only">{text("open")}</span></th></tr></thead><tbody>{entries.map(e => <tr key={e.id} className={picked === e.id ? "selected" : ""} onClick={() => setPicked(e.id)} onDoubleClick={() => enter(e)}><td><button className="drive-filename" onClick={() => { setPicked(e.id); if (e.kind === "folder") enter(e); }}><FileIcon kind={e.kind} /><span>{entryName(e)}</span></button></td><td>{e.date.slice(5).replace("-", "/")}</td><td>{e.size}</td><td><button className="drive-icon-button" aria-label={`${text("open")} ${entryName(e)}`} onClick={() => enter(e)}><ChevronRight size={15} /></button></td></tr>)}</tbody></table></div> : <div className="drive-file-grid">{entries.map(e => <button key={e.id} className={picked === e.id ? "selected" : ""} onClick={() => { setPicked(e.id); if (e.kind === "folder") enter(e); }} onDoubleClick={() => enter(e)}><FileIcon kind={e.kind} large /><strong>{entryName(e)}</strong><small>{e.kind === "folder" ? text("folder") : e.size}</small></button>)}</div>}
          <footer className="drive-file-footer"><span>{t("drive.itemCount", { count: entries.length })}</span><span><ShieldCheck size={13} />{text("originalLocation")}</span></footer>
        </div><aside className="drive-inspector" aria-label={text("details")}>{entry ? <><div className="drive-inspector-label">{text("details")}<span>{entry.kind === "folder" ? text("folder") : entry.name.split(".").at(-1)?.toUpperCase()}</span></div><div className="drive-detail-file"><FileIcon kind={entry.kind} large /><h3>{entryName(entry)}</h3><span>{entry.kind === "folder" ? text("folder") : entry.size}</span></div><dl><div><dt>{text("source")}</dt><dd>{drive.name}</dd></div><div><dt>{text("modified")}</dt><dd>{entry.date}</dd></div><div><dt>{text("access")}</dt><dd>{text(drive.writable ? "readWrite" : "readOnly")}</dd></div></dl><button className="pf-button drive-wide" onClick={() => enter(entry)}>{entry.kind === "folder" ? <Folder size={15} /> : <FileText size={15} />}{text(entry.kind === "folder" ? "openFolder" : "previewFile")}</button>{entry.kind !== "folder" && <button className="pf-button drive-wide" onClick={() => transfer(entry.name, "download")}><ArrowDownToLine size={15} />{text("download")}</button>}<div className="drive-ai-card"><Sparkles size={19} /><strong>{text("aiTitle")}</strong><p>{text(drive.ai ? "aiDescription" : "aiDisabled")}</p><button disabled={!drive.ai || entry.kind !== "text"} onClick={() => setAiOpen(true)}>{text("askAi")}<ChevronRight size={14} /></button></div></> : <div className="drive-inspector-empty"><FileText size={30} /><p>{text("selectFile")}</p><small>{text("selectFileBody")}</small></div>}</aside></div>
      </>}
    </section>
    <footer className="drive-bottom-note"><LockKeyhole size={13} />{text("privacyNote")}</footer>
    {notice && <div className="drive-notice" role="status"><CheckCheck size={17} />{notice}<button className="drive-icon-button" aria-label={text("dismiss")} onClick={() => setNotice("")}><X size={15} /></button></div>}
    {adding && <AddDrive onClose={() => setAdding(false)} onAdd={newDrive => { setDrives([...drives, newDrive]); setFiles({ ...files, [newDrive.id]: [] }); setFilter("all"); choose(newDrive); setAdding(false); setNotice(text("addedPreview")); }} />}
    {settings && <ConnectionSettings drive={drive} onClose={() => setSettings(false)} onSave={next => { setDrives(drives.map(d => d.id === next.id ? next : d)); setSettings(false); setNotice(text("settingsSaved")); }} />}
    {folderOpen && <Modal title={text("newFolder")} onClose={() => setFolderOpen(false)}><form className="drive-form" onSubmit={createFolder}><label>{text("folderName")}<input autoFocus value={folderName} onChange={e => setFolderName(e.target.value)} maxLength={100} placeholder={text("folderPlaceholder")} /></label>{folderError && <p role="alert">{folderError}</p>}<div className="drive-dialog-actions"><button type="button" className="pf-button" onClick={() => setFolderOpen(false)}>{text("cancel")}</button><button className="pf-button primary" type="submit">{text("create")}</button></div></form></Modal>}
    {transfersOpen && <Modal title={text("transfers")} onClose={() => setTransfersOpen(false)}><div className="drive-transfer-panel"><p className="drive-dialog-description">{text("transferPreview")}</p>{transfers.length === 0 && <div className="drive-empty"><CheckCheck size={28} /><p>{text("noTransfers")}</p></div>}{transfers.map(tr => <div className="drive-transfer" key={tr.id}><div className="drive-transfer-heading"><span className="drive-file-icon text">{tr.kind === "upload" ? <Upload size={20} /> : <ArrowDownToLine size={20} />}</span><div><strong>{tr.name}</strong><small>{tr.kind === "upload" ? `${text("thisDevice")} → ${tr.source}` : `${tr.source} → ${text("thisDevice")}`}</small></div><button className="drive-icon-button" aria-label={`${text("removeTask")} ${tr.name}`} onClick={() => setTransfers(transfers.filter(x => x.id !== tr.id))}><X size={16} /></button></div><div className="drive-transfer-progress"><span style={{ width: `${tr.progress}%` }} /></div><div className="drive-transfer-bottom"><span>{text(tr.state)} · {tr.progress}%</span><div>{tr.state !== "done" && <><button onClick={() => setTransfers(transfers.map(x => x.id === tr.id ? { ...x, state: x.state === "running" ? "paused" : "running" } : x))}>{tr.state === "running" ? <Pause size={13} /> : <Play size={13} />}{text(tr.state === "running" ? "pause" : "resume")}</button><button onClick={() => setTransfers(transfers.map(x => x.id === tr.id ? { ...x, progress: 100, state: "done" } : x))}>{text("simulateComplete")}</button></>}</div></div></div>)}</div></Modal>}
    {preview && <Modal title={entryName(preview)} onClose={() => setPreview(null)}><div className="drive-document-preview"><span className="drive-preview-pill">{text("sampleContent")}</span>{preview.kind === "text" ? <article><h2>{text("documentTitle")}</h2><p>{text("documentIntro")}</p><h3>{text("documentSection")}</h3><ul><li>{text("documentLine1")}</li><li>{text("documentLine2")}</li><li>{text("documentLine3")}</li></ul><blockquote>{text("documentQuote")}</blockquote></article> : <div className="drive-empty"><FileIcon kind={preview.kind} large /><h3>{text("filePreviewPlaceholder")}</h3><p>{text("filePreviewBody")}</p></div>}</div></Modal>}
    {aiOpen && entry && <Modal title={text("askAi")} onClose={() => setAiOpen(false)}><div className="drive-ai-preview"><span className="drive-preview-pill">{text("aiPreviewNote")}</span><div className="drive-ai-attachment"><FileText size={18} /><span>{entry.name}</span><Check size={15} /></div><p>{text("aiPrompt")}</p><div className="drive-ai-answer"><Sparkles size={19} /><p>{text("aiSampleAnswer")}</p></div><small>{text("aiPermissionNote")}</small></div></Modal>}
  </div>;
}

function PermissionFields({ writable, ai, setWritable, setAi }: { writable: boolean; ai: boolean; setWritable: (v: boolean) => void; setAi: (v: boolean) => void }) {
  const { t } = useTranslation(); const text = (key: string) => t(`drive.${key}`);
  return <div className="drive-permissions"><label className="drive-switch-row"><span><strong>{text("allowWrite")}</strong><small>{text("allowWriteHelp")}</small></span><input type="checkbox" role="switch" checked={writable} onChange={e => setWritable(e.target.checked)} /><span className="drive-switch" aria-hidden="true" /></label><label className="drive-switch-row"><span><strong>{text("allowAi")}</strong><small>{text("allowAiHelp")}</small></span><input type="checkbox" role="switch" checked={ai} onChange={e => setAi(e.target.checked)} /><span className="drive-switch" aria-hidden="true" /></label></div>;
}
function AddDrive({ onClose, onAdd }: { onClose: () => void; onAdd: (drive: Drive) => void }) {
  const { t } = useTranslation(); const text = (key: string) => t(`drive.${key}`);
  const [provider, setProvider] = useState<Provider | null>(null);
  const [name, setName] = useState(""); const [writable, setWritable] = useState(false); const [ai, setAi] = useState(false);
  const [protocol, setProtocol] = useState("SMB");
  return <Modal title={text("add")} onClose={onClose}><div className="drive-add-dialog"><div className="drive-steps"><span className={!provider ? "active" : "complete"}><i>{provider ? <Check size={12} /> : "1"}</i>{text("chooseSource")}</span><span className="drive-step-line" /><span className={provider ? "active" : ""}><i>2</i>{text("connectAndPermissions")}</span></div>{!provider ? <><p className="drive-dialog-description">{text("addDescription")}</p><div className="drive-provider-options">{(["nas", "google", "onedrive", "baidu"] as Provider[]).map(p => <button key={p} onClick={() => { setProvider(p); setName(p === "nas" ? text("homeNas") : p === "baidu" ? text("baidu") : providerNames[p]); }}><Brand provider={p} /><strong>{p === "nas" ? text("localNas") : p === "baidu" ? text("baidu") : providerNames[p]}</strong><small>{text(p === "nas" ? "nasProviderHelp" : p === "baidu" ? "baiduProviderHelp" : "cloudProviderHelp")}</small><ChevronRight size={16} /></button>)}</div><div className="drive-dialog-footnote"><ShieldCheck size={15} />{text("addPrivacy")}</div></> : <form className="drive-form" onSubmit={e => { e.preventDefault(); onAdd({ id: crypto.randomUUID(), provider, name: name.trim(), account: provider === "nas" ? `${text("thisDevice")} · ${protocol}` : "alex@example.com", used: 0, total: provider === "nas" ? 4000 : 100, state: "online", writable, ai }); }}><button className="drive-back" type="button" onClick={() => setProvider(null)}><ArrowLeft size={14} />{text("changeSource")}</button><div className="drive-connect-provider"><Brand provider={provider} /><strong>{provider === "baidu" ? text("baidu") : providerNames[provider]}</strong><span className="drive-preview-pill">{text("preview")}</span></div><label>{text("displayName")}<input required value={name} onChange={e => setName(e.target.value)} maxLength={60} /></label>{provider === "nas" ? <><label>{text("protocol")}<select value={protocol} onChange={e => setProtocol(e.target.value)}><option>{"SMB"}</option><option>{"WebDAV"}</option></select></label><div className="drive-form-pair"><label>{text(protocol === "SMB" ? "host" : "serverUrl")}<input placeholder={protocol === "SMB" ? "192.168.1.10" : "https://nas.example.com/dav"} /></label><label>{text("rootFolder")}<input placeholder={"/Public"} /></label></div><p className="drive-inline-help">{text("nasPreviewHelp")}</p></> : <div className="drive-auth-help"><LockKeyhole size={18} /><p>{text(provider === "baidu" ? "baiduAuthHelp" : "oauthHelp")}</p></div>}<PermissionFields writable={writable} ai={ai} setWritable={setWritable} setAi={setAi} /><div className="drive-dialog-actions"><button className="pf-button" type="button" onClick={onClose}>{text("cancel")}</button><button className="pf-button primary" type="submit" disabled={!name.trim()}>{text("simulateAdd")}<ChevronRight size={15} /></button></div></form>}</div></Modal>;
}
function ConnectionSettings({ drive, onClose, onSave }: { drive: Drive; onClose: () => void; onSave: (drive: Drive) => void }) {
  const { t } = useTranslation(); const text = (key: string) => t(`drive.${key}`);
  const [name, setName] = useState(drive.name); const [writable, setWritable] = useState(drive.writable); const [ai, setAi] = useState(drive.ai); const [state, setState] = useState(drive.state);
  return <Modal title={text("connectionSettings")} onClose={onClose}><form className="drive-form" onSubmit={e => { e.preventDefault(); onSave({ ...drive, name: name.trim(), writable, ai, state }); }}><div className="drive-connect-provider"><Brand provider={drive.provider} /><div><strong>{drive.name}</strong><small>{drive.account}</small></div><span className={`drive-status ${state}`}><i />{text(state === "online" ? "connected" : "reauthNeeded")}</span></div>{state === "expired" && <div className="drive-auth-help"><LockKeyhole size={18} /><div><p>{text("expiredBody")}</p><button type="button" className="pf-button" onClick={() => setState("online")}>{text("simulateAuth")}</button></div></div>}<label>{text("displayName")}<input required maxLength={60} value={name} onChange={e => setName(e.target.value)} /></label><div className="drive-connection-device"><Monitor size={19} /><div><strong>{text("accessDevice")}</strong><small>{drive.provider === "nas" ? drive.account.split(" · ")[0] + " · " + text("deviceMustBeOnline") : text("thisDevice")}</small></div></div><PermissionFields writable={writable} ai={ai} setWritable={setWritable} setAi={setAi} /><p className="drive-inline-help">{text("settingsPreviewHelp")}</p><div className="drive-dialog-actions"><button type="button" className="pf-button" onClick={onClose}>{text("cancel")}</button><button className="pf-button primary" type="submit" disabled={!name.trim()}>{text("save")}</button></div></form></Modal>;
}
