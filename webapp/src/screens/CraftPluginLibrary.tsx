import { useState } from "react";
import { useTranslation } from "react-i18next";
import { ArrowRight, ArrowUpRight, Bot, Check, ChevronRight, Ellipsis, Info, Layers3, Plus, Search, ShieldCheck, Trash2, X } from "lucide-react";
import type { CraftCatalog, CraftPlugin } from "../domain/craftPlugins";
import { isTauriDesktop } from "../domain/nodeUrl";
import CraftPluginIcon from "./CraftPluginIcon";
import "./craftPluginLibrary.css";

type Props = {
  catalog: CraftCatalog; busy: boolean;
  onAdd: (ids?: string[]) => void; onOpen: (app: CraftPlugin) => void;
  onToggle: (app: CraftPlugin) => void; onDetails: (app: CraftPlugin) => void;
  onUninstall: (app: CraftPlugin) => void; onAgent: (app: CraftPlugin) => void;
};

export default function CraftPluginLibrary({ catalog, busy, onAdd, onOpen, onToggle, onDetails, onUninstall, onAgent }: Props) {
  const { t } = useTranslation();
  const text = (key: string, options?: Record<string, unknown>) => String(t(`craft.${key}`, options));
  const [view, setView] = useState<"installed" | "discover">("installed");
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("all");
  const installed = catalog.plugins.filter(app => app.installed);
  const available = catalog.plugins.filter(app => !app.installed);
  const search = query.trim().toLocaleLowerCase();
  const filtered = (view === "installed" ? installed : available).filter(app =>
    (view !== "discover" || category === "all" || app.group === category) &&
    (!search || [text(app.id), app.name, text(`descriptions.${app.id}`, { defaultValue: app.name }), ...app.formats].join(" ").toLocaleLowerCase().includes(search)));
  const browse = () => { setView("discover"); setQuery(""); setCategory("all"); };

  return <div className="craft-library">
    <header className="craft-library-heading">
      <div><div className="craft-library-eyebrow"><Layers3 size={14} />{text("yourToolkit")}</div><h1>{text("plugins")}</h1><p>{text("libraryIntro")}</p></div>
      <button className="craft-library-add" disabled={busy} onClick={() => onAdd()}><Plus size={17} />{text("add")}</button>
    </header>

    <div className="craft-library-toolbar">
      <nav className="craft-library-tabs" aria-label={text("pluginViews")}>
        <button aria-pressed={view === "installed"} onClick={() => setView("installed")}>{text("installed")}<span>{installed.length}</span></button>
        <button aria-pressed={view === "discover"} onClick={() => setView("discover")}>{text("discover")}<span>{available.length}</span></button>
      </nav>
      <div className="craft-library-search"><Search size={16} /><input type="search" aria-label={text("searchPlugins")} placeholder={text("searchPlaceholder")} value={query} onChange={e => setQuery(e.target.value)} />{query && <button aria-label={text("clearSearch")} onClick={() => setQuery("")}><X size={14} /></button>}</div>
    </div>

    {view === "discover" && <nav className="craft-library-categories" aria-label={text("pluginCategories")}>{["all", "office", "workflow", "design", "media", "storage"].map(id => <button key={id} aria-pressed={category === id} onClick={() => setCategory(id)}>{text(id)}</button>)}</nav>}

    {filtered.length ? <div className="craft-library-grid">{filtered.map(app => {
      const label = text(app.id);
      const builtin = app.kind === "builtin_adapter";
      const installable = app.installable ?? app.web;
      const capability = !app.enabled ? text("agentWithPlugin") : builtin ? app.id === "nas" ? text("scopedFiles") : text("businessOperations", { count: app.agent_tools.length }) : text("infoTool");
      return <article className={`craft-library-card ${app.installed && !app.enabled ? "is-disabled" : ""}`} data-tool={app.id} key={app.id}>
        <div className="craft-library-card-heading"><CraftPluginIcon app={app} /><div><h3>{label}</h3><p>{app.name}<span>·</span>{app.version}</p></div><details className="craft-library-menu"><summary aria-label={text("more", { name: label })}><Ellipsis size={19} /></summary><div><button onClick={() => onDetails(app)}><Info size={15} />{text("details")}</button>{app.installed && <button className="danger" disabled={busy || app.busy} onClick={() => onUninstall(app)}><Trash2 size={15} />{text("uninstall")}</button>}</div></details></div>
        <p className="craft-library-description">{text(`descriptions.${app.id}`, { defaultValue: app.name })}</p>
        <div className="craft-library-meta"><span>{text(builtin ? "builtinLabel" : app.web ? isTauriDesktop() ? "desktopLabel" : "browserLabel" : "native")}</span>{app.installed && <><i /><button disabled={!app.enabled || !app.agent_tools.length} onClick={() => onAgent(app)}><Bot size={14} />{capability}</button></>}</div>
        {app.warning && <p className="craft-library-warning"><Info size={13} />{text(app.warning === "cjk_font_coverage" ? "cjkWarningShort" : "fontWarningShort")}</p>}
        <footer>{app.installed ? <>
          <button className="craft-library-open" disabled={busy || app.busy || !app.enabled || !installable} onClick={() => onOpen(app)}>{text("open")}<ArrowUpRight size={15} /></button>
          <label className="craft-library-toggle"><span>{text(app.busy ? "busy" : app.enabled ? "enabled" : "disabled")}</span><input role="switch" type="checkbox" aria-label={`${text(app.enabled ? "disable" : "enable")} ${label}`} checked={app.enabled} disabled={busy || app.busy} onChange={() => onToggle(app)} /></label>
        </> : <><span className="craft-library-install-note">{builtin ? <><Check size={13} />{text("noDownload")}</> : installable ? text("onDemand") : text("nativeUnavailable")}</span><button className="craft-library-install" disabled={busy || app.busy} onClick={() => installable ? onAdd([app.id]) : onDetails(app)}>{text(installable ? "install" : "details")}<span>{installable ? <Plus size={15} /> : <ChevronRight size={15} />}</span></button></>}</footer>
      </article>;
    })}</div> : <div className="craft-library-empty"><div><Layers3 size={32} strokeWidth={1.3} /></div><h2>{text(search ? "noMatches" : view === "installed" ? "noPlugins" : "allInstalled")}</h2><p>{text(search ? "searchHelp" : view === "installed" ? "emptyHelp" : "allInstalledHelp")}</p><button className="craft-button" onClick={search ? () => setQuery("") : browse}>{text(search ? "clearSearch" : "discover")}<ArrowRight size={15} /></button></div>}

    {view === "installed" && installed.length > 0 && !search && available.some(app => app.installable ?? app.web) && <button className="craft-library-discover" onClick={browse}><span className="craft-library-mini-icons">{catalog.plugins.filter(app => !app.installed && (app.installable ?? app.web)).slice(0, 3).map(app => <CraftPluginIcon key={app.id} app={app} />)}</span><span><strong>{text("discoverMore")}</strong><small>{text("discoverHelp")}</small></span><ArrowRight size={19} /></button>}

    <footer className="craft-library-foot"><p><ShieldCheck size={16} />{text("libraryFootnote")}</p><details className="craft-library-storage"><summary>{text("storage")}<ChevronRight size={14} /></summary><div><code>{catalog.storage_path}</code><p>{text("storageHelp")}</p></div></details></footer>
  </div>;
}
