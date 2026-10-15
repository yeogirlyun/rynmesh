import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowRight, ArrowUpRight, Bot, CheckCircle2, Download, FileUser, FolderCheck, HardDrive, Headset, Plus, UsersRound } from "lucide-react";
import { Modal } from "../personal/components";
import { craftPlugins, openCraftWindow, verifiedCraftUrl, verifiedPluginPath, type CraftCatalog, type CraftPlugin } from "../domain/craftPlugins";
import { nasChanged, setNasHandoff } from "../domain/nas";
import CraftPluginLibrary from "./CraftPluginLibrary";
import CraftPluginIcon from "./CraftPluginIcon";
import { isTauriDesktop } from "../domain/nodeUrl";
import "./craftApps.css";

const short: Record<string, string> = { pdfcraft: "PDF", wordcraft: "W", gridcraft: "X", deckcraft: "P", resume: "R", recruit: "H", desk: "D", nas: "N" };
const isBuiltin = (app: CraftPlugin) => app.kind === "builtin_adapter";
const canInstall = (app: CraftPlugin) => app.installable ?? app.web;
const extension: Record<string, string> = { pdfcraft: "PDF", wordcraft: "DOCX", gridcraft: "XLSX", deckcraft: "PPTX" };
function Mark({ app }: { app: CraftPlugin }) {
  return <CraftPluginIcon app={app} />;
}
function Paper({ app }: { app: CraftPlugin }) {
  if (isBuiltin(app)) {
    const Icon = app.id === "nas" ? HardDrive : app.id === "resume" ? FileUser : app.id === "recruit" ? UsersRound : Headset;
    return <span className="craft-builtin-preview" aria-hidden="true"><Icon size={35} strokeWidth={1.2} /><span><i /><i /><i /></span></span>;
  }
  if (app.id === "gridcraft") return <span className="craft-sheet" aria-hidden="true"><i /><span>{Array.from({ length: 16 }, (_, n) => <i key={n} className={[4, 5, 8, 9].includes(n) ? "selected" : ""} />)}</span></span>;
  if (app.id === "deckcraft") return <span className="craft-slide" aria-hidden="true"><i /><i /><i /><b /></span>;
  return <span className="craft-paper" aria-hidden="true"><b>{short[app.id] || app.name.slice(0, 1)}</b><i /><i /><i /><i /></span>;
}

export default function CraftApps({ manage = false }: { manage?: boolean }) {
  const { t } = useTranslation();
  const text = (key: string, options?: Record<string, unknown>) => String(t(`craft.${key}`, options));
  const name = (app: CraftPlugin) => text(app.id);
  const warningText = (app: CraftPlugin) => text(app.warning === "cjk_font_coverage" ? "cjkWarning" : "fontWarning");
  const warningShortText = (app: CraftPlugin) => text(app.warning === "cjk_font_coverage" ? "cjkWarningShort" : "fontWarningShort");
  const navigate = useNavigate();
  const [catalog, setCatalog] = useState<CraftCatalog | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [group, setGroup] = useState("office");
  const [setup, setSetup] = useState<"initial" | "add" | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [installing, setInstalling] = useState("");
  const [installedBatch, setInstalledBatch] = useState<string[]>([]);
  const [modal, setModal] = useState<{ kind: "disable" | "uninstall" | "detail" | "agent"; app: CraftPlugin } | null>(null);
  const [ready, setReady] = useState<{ app: CraftPlugin; url: string } | null>(null);
  const [clearPreferences, setClearPreferences] = useState(false);
  const mounted = useRef(true);
  const operation = useRef(false);
  const failure = (e: unknown) => {
    const code = e instanceof Error ? e.message : "plugin_operation_failed";
    setError(code === "plugin_local_browser_required" ? text("localBrowserRequired") : text(`errors.${code}`, { defaultValue: text("errors.plugin_operation_failed") }));
  };
  const load = async () => {
    setError("");
    try {
      const data = await craftPlugins.catalog();
      if (!mounted.current) return;
      setCatalog(data);
      if (data.migration_error) failure(new Error(data.migration_error));
      if (!manage && !data.setup_completed) { setSetup("initial"); setSelected((data.selection || data.defaults).filter(id => !data.plugins.find(p => p.id === id)?.installed)); }
    } catch (e) { if (mounted.current) failure(e); }
  };
  useEffect(() => { mounted.current = true; void load(); return () => { mounted.current = false; }; }, [manage]); // Loading never installs or launches anything.
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 5000);
    return () => window.clearTimeout(timer);
  }, [notice]);
  const run = async (fn: () => Promise<void>) => {
    if (operation.current) return;
    operation.current = true;
    setBusy(true); setError(""); setNotice("");
    try { await fn(); } catch (e) { if (mounted.current) failure(e); }
    finally { operation.current = false; if (mounted.current) setBusy(false); }
  };
  const add = (ids: string[] = []) => { setSetup("add"); setSelected(ids); setInstalledBatch([]); setNotice(""); setError(""); };
  const install = () => run(async () => {
    let latest = catalog!;
    for (const id of selected) {
      if (latest.plugins.find(p => p.id === id)?.installed) continue;
      setInstalling(id);
      try { latest = await craftPlugins.install(id); }
      catch (e) { setNotice(text("installFailed", { name: text(id) })); throw e; }
      if (mounted.current) { setCatalog(latest); setInstalledBatch(current => [...current, id]); if (id === "nas") nasChanged(); }
    }
    if (setup === "initial") latest = await craftPlugins.setup();
    if (mounted.current) { setCatalog(latest); setInstalling(""); setSetup(null); setNotice(text("installSuccess")); }
  });
  const dismissSetup = () => run(async () => {
    if (setup === "initial") setCatalog(await craftPlugins.setup());
    setSetup(null); setInstalling("");
  });
  const open = (app: CraftPlugin) => run(async () => {
    if (isBuiltin(app)) { navigate(verifiedPluginPath(app)); return; }
    setReady(null);
    // Clicking Open is the launch request. Validate both responses without an
    // extra confirmation step for an already installed and enabled editor.
    const next = await craftPlugins.preview(app.id);
    verifiedCraftUrl(next, app, catalog!.node_id);
    const value = await craftPlugins.launch(app.id, next.ticket);
    const url = verifiedCraftUrl(value, app, catalog!.node_id);
    await openCraftWindow(app.id, url);
    if (mounted.current) { setReady({ app, url }); setNotice(text(isTauriDesktop() ? "desktopReady" : "ready")); }
  });
  const change = () => run(async () => {
    if (!modal || (modal.kind !== "disable" && modal.kind !== "uninstall")) return;
    const value = modal.kind === "uninstall" ? await craftPlugins.uninstall(modal.app.id, clearPreferences) : await craftPlugins.enable(modal.app.id, false);
    setCatalog(value); setReady(null); setModal(null); setNotice(text(modal.kind === "uninstall" ? "uninstalled" : "saved"));
    if (modal.app.id === "nas") { setNasHandoff(null); nasChanged(); }
  });
  const choose = (app: CraftPlugin) => <label className="craft-choice" key={app.id} data-tool={app.id}>
    <Mark app={app} /><span><strong>{name(app)}</strong><small>{app.installed ? text("installed") : !canInstall(app) ? text(isBuiltin(app) ? "unavailable" : "native") : isBuiltin(app) ? text("builtinType") : text(`descriptions.${app.id}`, { defaultValue: app.name })}</small>{app.warning && <small>{warningShortText(app)}</small>}</span>
    <input type="checkbox" checked={selected.includes(app.id) || app.installed} disabled={busy || !canInstall(app) || app.installed} onChange={e => setSelected(current => e.target.checked ? [...current, app.id] : current.filter(id => id !== app.id))} />
  </label>;
  const hasDownloads = selected.some(id => catalog?.plugins.some(app => app.id === id && app.web && !app.installed));
  return <section className={`craft-center${manage ? " craft-manager" : ""}`} aria-busy={busy}>
    {error && <div className="craft-error" role="alert"><span>{error}</span>{!catalog && <button onClick={() => void load()}>{text("retry")}</button>}</div>}
    {notice && <p className="craft-notice" role="status"><CheckCircle2 size={17} />{notice}</p>}
    {!catalog && !error && <p role="status">{text("loading")}</p>}
    {catalog && (setup ? <div className="craft-installer">
      <aside><div className="craft-brand">Ryn</div><h2>{text("storyTitle")}</h2><p>{text("storyHelp")}</p><div className="craft-stack">{catalog.plugins.filter(app => catalog.defaults.includes(app.id)).map(app => <div key={app.id} data-tool={app.id}><Paper app={app} /></div>)}</div></aside>
      <div className="craft-install-body"><h1>{text(setup === "initial" ? "choose" : "add")}</h1><p className="craft-muted">{text(setup === "initial" ? "chooseHelp" : "addHelp")}</p>
        <div className="craft-choices">{catalog.plugins.filter(app => app.group === "office").map(choose)}</div>
        <details className="craft-other"><summary>{text("otherTools")}</summary>{["workflow", "design", "media", "storage"].map(category => { const entries = catalog.plugins.filter(app => app.group === category); return entries.length ? <section className="craft-choice-group" key={category}><h3>{text(category)}</h3><div className="craft-extra">{entries.map(choose)}</div></section> : null; })}</details>
        <p className="craft-note"><Bot size={16} />{text("enableInfo")}</p>
        {busy && installing && <p className="craft-download" role="status"><Download size={16} />{text(catalog.plugins.find(app => app.id === installing)?.web ? "downloading" : "registering", { name: text(installing) })}</p>}
        {installedBatch.length > 0 && <p className="craft-muted">{installedBatch.map(id => text(id)).join(" · ")} — {text("installed")}</p>}
        <footer><div>{text("selected", { count: selected.filter(id => !catalog.plugins.find(p => p.id === id)?.installed).length })}<small>{text(hasDownloads ? "downloadHelp" : "registrationHelp")}</small></div><div className="craft-actions"><button className="craft-button quiet" disabled={busy} onClick={() => void dismissSetup()}>{text(setup === "initial" ? "later" : "back")}</button><button className="craft-button primary" disabled={busy || (setup === "add" && !selected.some(id => !catalog.plugins.find(p => p.id === id)?.installed))} onClick={() => void install()}>{text(busy ? "busy" : selected.length ? hasDownloads ? "download" : "installBuiltin" : "finish")}</button></div></footer>
      </div>
    </div> : <>
      {!manage && <header className="craft-heading"><div><h1>{text("title")}</h1><p>{text("subtitle")}</p></div><Link className="craft-button quiet" to="/settings/plugins">{text("manage")}<ArrowUpRight size={15} /></Link></header>}
      {manage ? <CraftPluginLibrary catalog={catalog} busy={busy} onAdd={add} onOpen={app => void open(app)}
        onToggle={app => app.enabled ? setModal({ kind: "disable", app }) : void run(async () => { setCatalog(await craftPlugins.enable(app.id, true)); if (app.id === "nas") nasChanged(); setNotice(text("saved")); })}
        onDetails={app => setModal({ kind: "detail", app })} onAgent={app => setModal({ kind: "agent", app })}
        onUninstall={app => { setClearPreferences(false); setModal({ kind: "uninstall", app }); }} /> : <>
        <nav className="craft-tabs" aria-label={text("title")}>{["office", "workflow", "design", "media", "storage"].map(id => <button key={id} aria-pressed={group === id} onClick={() => setGroup(id)}>{text(id)}</button>)}</nav>
        <div className="craft-grid">{catalog.plugins.filter(app => app.group === group).map(app => <article className="craft-tool" key={app.id} data-tool={app.id}>
          <button className="craft-cover" aria-label={`${text(app.installed ? "open" : "install")} ${name(app)}`} disabled={busy || !canInstall(app) || (app.installed && !app.enabled)} onClick={() => app.installed ? void open(app) : add([app.id])}><Paper app={app} /><small>{extension[app.id] || app.name}</small></button>
          <h3>{name(app)}</h3><p>{!canInstall(app) ? text(isBuiltin(app) ? "unavailable" : "native") : app.installed && !app.enabled ? text("disabled") : text(`descriptions.${app.id}`, { defaultValue: app.name })}</p>{app.warning && <p>{warningShortText(app)}</p>}
          <div className="craft-tool-actions">{app.installed ? <button disabled={busy || !app.enabled} onClick={() => void open(app)}>{text("open")}<ArrowUpRight size={15} /></button> : <button disabled={busy || !canInstall(app)} onClick={() => add([app.id])}>{text("install")}<Plus size={15} /></button>}
          <button disabled={!app.enabled || !app.agent_tools.length} onClick={() => setModal({ kind: "agent", app })}>{text("agent")}<ArrowRight size={15} /></button></div>
        </article>)}</div><p className="craft-note"><Bot size={16} />{text("enableInfo")}</p><p className="craft-foot">{text("retention")}</p>
        <Link className="craft-legacy" to="/apps/workspaces">{text("legacy")}<ArrowUpRight size={14} /></Link>
      </>}
      {ready && <div className="craft-ready"><FolderCheck size={18} /><span>{name(ready.app)}</span><a href={ready.url} target="_blank" rel="noopener noreferrer" onClick={e => { if ("__TAURI_INTERNALS__" in window) { e.preventDefault(); void openCraftWindow(ready.app.id, ready.url).catch(failure); } }}>{text("openWindow")}<ArrowUpRight size={15} /></a></div>}
    </>)}
    {modal && <Modal title={modal.kind === "detail" ? name(modal.app) : modal.kind === "agent" ? `${name(modal.app)} · Agent` : text(`${modal.kind}Title`, { name: name(modal.app) })} onClose={() => { if (!busy) setModal(null); }} className="craft-dialog">
      {modal.kind === "disable" ? <p>{text("disableHelp")}</p> : modal.kind === "uninstall" ? <><p>{text(isBuiltin(modal.app) ? "builtinUninstallHelp" : "uninstallHelp")}</p><div className="craft-dialog-note"><FolderCheck size={17} />{text("uninstallKeep")}</div><label className="craft-clear"><input type="checkbox" disabled={busy} checked={clearPreferences} onChange={e => setClearPreferences(e.target.checked)} />{text("clearPreferences")}</label></> : modal.kind === "agent" ? <><p>{text("enableInfo")}</p><div className="craft-dialog-note"><Bot size={17} />{text(isBuiltin(modal.app) ? modal.app.id === "nas" ? "nasAgentInfo" : "businessAgentInfo" : "agentInfo", { count: modal.app.agent_tools.length })}</div></> : <><dl><div><dt>{text("version")}</dt><dd>{modal.app.name} {modal.app.version}</dd></div><div><dt>{text("source")}</dt><dd>{modal.app.source_url}</dd></div><div><dt>{text("checksum")}</dt><dd><code>{modal.app.sha256}</code></dd></div>{modal.app.formats.length > 0 && <div><dt>{text("formats")}</dt><dd>{modal.app.formats.join(", ")}</dd></div>}<div><dt>{text("tools")}</dt><dd>{modal.app.enabled ? text(isBuiltin(modal.app) ? "builtinCapabilities" : "infoTool", { count: modal.app.agent_tools.length }) : text("disabled")}</dd></div></dl><p>{text(isBuiltin(modal.app) ? "builtinHelp" : "formatsHelp")}</p><p>{text(isBuiltin(modal.app) ? "builtinLicense" : "license")}</p>{modal.app.warning && <p>{warningText(modal.app)}</p>}</>}
      {error && <p role="alert" className="craft-error">{error}</p>}
      <div className="craft-dialog-actions"><button className="craft-button" disabled={busy} onClick={() => setModal(null)}>{text(modal.kind === "detail" || modal.kind === "agent" ? "close" : "cancel")}</button>
        {modal.kind === "agent" ? <button className="craft-button primary" onClick={() => navigate("/agent")}>{text("agent")}<ArrowRight size={15} /></button> : modal.kind !== "detail" && <button className={`craft-button ${modal.kind === "uninstall" ? "danger-fill" : "primary"}`} disabled={busy} onClick={() => void change()}>{text(busy ? "busy" : modal.kind)}</button>}
      </div>
    </Modal>}
  </section>;
}
