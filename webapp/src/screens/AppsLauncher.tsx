import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowRight, ArrowUpRight, Bot, Building2, Check, Info, Pin, PinOff, Plus } from "lucide-react";
import type { ExtensionOverview } from "../domain/agentExtensions";
import type { CraftCatalog, CraftPlugin } from "../domain/craftPlugins";
import { tr } from "../uiI18n";
import AppIcon, { appTone } from "./appIcons";
import { pinnedPluginWorkspaces, PIN_CHANGE_EVENT, pluginWorkspacePath, setPluginWorkspacePinned } from "./pluginWorkspaceNavigation";
import "./craft-apps-refresh.css";

const groups = ["office", "workflow", "design", "media", "storage"] as const;
export type AppsFilter = "all" | (typeof groups)[number];
const isBuiltin = (app: CraftPlugin) => app.kind === "builtin_adapter";
const canInstall = (app: CraftPlugin) => app.installable ?? app.web;

type Props = {
  embedded?: boolean;
  catalog: CraftCatalog;
  extensionOverview?: ExtensionOverview | null;
  onRefreshExtensions?: () => void;
  busy: boolean;
  /** Kept by the parent so it survives the installer replacing this view. */
  filter: AppsFilter;
  onFilterChange: (filter: AppsFilter) => void;
  /** Translates a key of the craft namespace. */
  text: (key: string, options?: Record<string, unknown>) => string;
  onOpen: (app: CraftPlugin) => void;
  onInstall: (app: CraftPlugin) => void;
  onAgent: (app: CraftPlugin) => void;
};

/** The Apps page: what is installed is one click from opening, what is not is one click from installing. */
export default function AppsLauncher({ embedded = false, catalog, extensionOverview, busy, filter, onFilterChange, text, onOpen, onInstall, onAgent }: Props) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const [pinnedPlugins, setPinnedPlugins] = useState(pinnedPluginWorkspaces);
  useEffect(() => {
    const sync = () => setPinnedPlugins(pinnedPluginWorkspaces());
    window.addEventListener(PIN_CHANGE_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(PIN_CHANGE_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);
  const isZh = (i18n.resolvedLanguage || i18n.language || "en").toLowerCase().startsWith("zh");
  const tx = (zh: string, en: string) => (isZh ? zh : en);
  const extensionPlugins = (extensionOverview?.plugins ?? []).filter(
    (p) => !p.builtin || (p.views?.length ?? 0) > 0,
  );
  const craftCatalogPlugins = embedded ? catalog.plugins.filter(app => app.installed) : catalog.plugins;
  const showFilters = !embedded || craftCatalogPlugins.length + extensionPlugins.length >= 8;
  const selectedFilter = showFilters ? filter : "all";
  const showExtensionPlugins =
    extensionPlugins.length > 0 && (selectedFilter === "all" || selectedFilter === "workflow");
  const name = (app: CraftPlugin) => text(app.id);
  const warning = (app: CraftPlugin) => text(app.warning === "cjk_font_coverage" ? "cjkWarningShort" : "fontWarningShort");
  const summary = (app: CraftPlugin) => text(`descriptions.${app.id}`, { defaultValue: app.name });
  // Why an app cannot be started or installed here, shown as a label rather than replacing its description.
  const limit = (app: CraftPlugin) => (canInstall(app) ? "" : text(isBuiltin(app) ? "unavailable" : "native"));
  const visible = craftCatalogPlugins.filter((app) => selectedFilter === "all" || app.group === selectedFilter);
  const installed = visible.filter((app) => app.installed);
  const notInstalled = visible.filter((app) => !app.installed);
  const count = (id: AppsFilter) => craftCatalogPlugins.filter(app => id === "all" || app.group === id).length
    + (embedded && (id === "all" || id === "workflow") ? extensionPlugins.length : 0);
  const installedTotal = craftCatalogPlugins.filter((app) => app.installed).length;
  // Office apps have their own Agent note when this build carries it; otherwise the general one.
  const info =
    (filter === "all" || filter === "office") && i18n.exists("craft.officeAgentInfo") ? "officeAgentInfo" : "enableInfo";
  return (
    <>
      {embedded && <style>{`
        .apps-hub-mine section:has(> [data-embedded-launcher]) {
          display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 220px), 1fr)); gap: 12px;
        }
        .apps-hub-mine section:has(> [data-embedded-launcher]) > h2 { grid-column: 1 / -1; margin: 0; }
        .apps-hub-mine section:has(> [data-embedded-launcher]) > .apps-hub-grid,
        .craft-center[data-embedded-launcher], [data-embedded-launcher] .ra-section,
        [data-embedded-launcher] .ra-grid4 { display: contents; }
        .craft-center[data-embedded-launcher]::before { content: none; }
        [data-embedded-launcher] > :not(.ra-section) { grid-column: 1 / -1; }
        [data-embedded-launcher] .apps-hub-card .ra-top { align-items: center; }
        [data-embedded-launcher] .apps-hub-card .ra-glyph { width: 34px; height: 34px; flex: none; }
        [data-embedded-launcher] .ra-top h3 { flex: 1; }
        [data-embedded-launcher] .ra-foot { flex-wrap: wrap; justify-content: start; padding: 0; }
        [data-embedded-launcher] .ra-slot { min-height: 0; }
        [data-embedded-launcher] .ra-slot:empty { display: none; }
      `}</style>}
      {!embedded && <header className="ra-head">
        <div>
          <h1>{text("title")}</h1>
          <p>{text("subtitle")}</p>
          <div className="ra-stats">
            <span className="ra-chip ok">
              <Check size={13} aria-hidden="true" />
              {tr("{{count}} installed", { count: installedTotal })}
            </span>
            <span className="ra-chip info">{tr("{{count}} not installed", { count: craftCatalogPlugins.length - installedTotal })}</span>
          </div>
        </div>
        <Link className="ra-btn" to="/settings/plugins">
          {text("manage")}
          <ArrowUpRight size={15} aria-hidden="true" />
        </Link>
      </header>}
      {showFilters && <nav className="ra-filters" aria-label={text("title")}>
        {(["all", ...groups] as const).map((id) => (
          <button key={id} type="button" aria-pressed={filter === id} className={filter === id ? "on" : ""} onClick={() => onFilterChange(id)}>
            {id === "all" ? t("personal.all") : text(id)}
            <em aria-hidden="true">{count(id)}</em>
          </button>
        ))}
      </nav>}
      {(installed.length > 0 || showExtensionPlugins) && (
        <section className="ra-section" aria-label={text("installed")}>
          {!embedded && <h2>
            {text("installed")} <span>({installed.length + (showExtensionPlugins ? extensionPlugins.length : 0)})</span>
          </h2>}
          <div className="ra-grid4">
            {showExtensionPlugins &&
              extensionPlugins.map((plugin) => {
                const servers = plugin.mcp_servers?.length ? plugin.mcp_servers : (extensionOverview?.mcp_servers ?? []).filter(m => m.plugin_id === plugin.id || plugin.mcp_server_ids.includes(m.id));
                const isLoggedIn = servers.some(s => s.oauth?.state === "logged_in");
                const hasOauth = servers.some(s => Boolean(s.oauth));
                const appPath = plugin.web_app ? `/apps/extensions/${encodeURIComponent(plugin.id)}` : pluginWorkspacePath(plugin.id);
                const pinned = pinnedPlugins.some(item => item.id === plugin.id);
                return (
                  <article
                    key={`ext-${plugin.id}`}
                    className={`${embedded ? "apps-hub-card" : "ra-card"} ra-t-teal${plugin.enabled ? "" : " off"}`}
                    data-extension-plugin={plugin.id}
                  >
                    <div className="ra-top">
                      <button
                        type="button"
                        className="ra-glyph"
                        aria-label={`${text("open")} ${plugin.title}`}
                        disabled={!plugin.enabled}
                        onClick={() => navigate(appPath)}
                      >
                        <Building2 size={22} color="#fff" />
                      </button>
                      {embedded && <h3>{plugin.title}</h3>}
                      {!plugin.enabled && <span className="ra-chip">{text("disabled")}</span>}
                      <button type="button" className="ra-btn ghost sm" aria-pressed={pinned}
                        title={pinned ? tx("取消固定", "Unpin") : tx("固定到侧栏", "Pin to sidebar")}
                        aria-label={`${pinned ? tx("取消固定", "Unpin") : tx("固定到侧栏", "Pin to sidebar")} ${plugin.title}`}
                        onClick={() => setPluginWorkspacePinned({ id: plugin.id, title: plugin.title }, !pinned)}>
                        {pinned ? <PinOff size={16} aria-hidden="true" /> : <Pin size={16} aria-hidden="true" />}
                      </button>
                    </div>
                    {!embedded && <div>
                      <h3>{plugin.title}</h3>
                      <p>
                        {plugin.description}
                      </p>
                    </div>}
                    <div className="ra-slot">
                      {isLoggedIn ? (
                        <span className="ra-chip ok">
                          <Check size={13} aria-hidden="true" />
                          {tx("已登录", "Signed in")}
                        </span>
                      ) : hasOauth ? (
                        <span className="ra-chip warn">{tx("需要登录", "Sign-in required")}</span>
                      ) : null}
                    </div>
                    <div className="ra-foot">
                      <button
                        type="button"
                        className="ra-btn primary sm"
                        disabled={!plugin.enabled}
                        onClick={() => navigate(appPath)}
                      >
                        {text("open")}
                        <ArrowUpRight size={14} aria-hidden="true" />
                      </button>
                      {plugin.peer_provider && <button
                        type="button"
                        className="ra-btn ghost sm"
                        disabled={!plugin.enabled}
                        onClick={() => navigate(`${appPath}?tab=peers`)}
                      >
                        {tx("成员与模型", "Members & models")}
                        <ArrowRight size={14} aria-hidden="true" />
                      </button>}
                      <button
                        type="button"
                        className="ra-btn ghost sm"
                        disabled={!plugin.enabled}
                        onClick={() =>
                          navigate("/agent")
                        }
                      >
                        {tx("打开 Agent", "Open Agent")}
                        <ArrowRight size={14} aria-hidden="true" />
                      </button>
                    </div>
                  </article>
                );
              })}
            {installed.map((app) => (
              <article className={`${embedded ? "apps-hub-card" : "ra-card"} ra-t-${appTone(app.id)}${app.enabled ? "" : " off"}`} key={app.id} data-tool={app.id}>
                <div className="ra-top">
                  <button
                    type="button"
                    className="ra-glyph"
                    aria-label={`${text("open")} ${name(app)}`}
                    disabled={busy || !canInstall(app) || !app.enabled}
                    onClick={() => onOpen(app)}
                  >
                    <AppIcon id={app.id} name={name(app)} />
                  </button>
                  {embedded && <h3>{name(app)}</h3>}
                  {(limit(app) || !app.enabled) && <span className="ra-chip">{limit(app) || text("disabled")}</span>}
                </div>
                {!embedded && <div>
                  <h3>{name(app)}</h3>
                  <p>{summary(app)}</p>
                </div>}
                <div className="ra-slot">
                  {app.warning && (
                    <span className="ra-chip warn">
                      <Info size={13} aria-hidden="true" />
                      {warning(app)}
                    </span>
                  )}
                </div>
                <div className="ra-foot">
                  <button type="button" className="ra-btn primary sm" disabled={busy || !app.enabled} onClick={() => onOpen(app)}>
                    {text("open")}
                    <ArrowUpRight size={14} aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    className="ra-btn ghost sm"
                    disabled={!app.enabled || !app.agent_tools.length}
                    onClick={() => onAgent(app)}
                  >
                    {text("agent")}
                    <ArrowRight size={14} aria-hidden="true" />
                  </button>
                </div>
              </article>
            ))}
          </div>
        </section>
      )}
      {notInstalled.length > 0 && (
        <section className="ra-section" aria-label={tr("Not installed")}>
          <h2>
            {tr("Not installed")} <span>({notInstalled.length})</span>
          </h2>
          <div className="ra-grid3">
            {notInstalled.map((app) => (
              <article className={`ra-row ra-t-${appTone(app.id)}`} key={app.id} data-tool={app.id}>
                <button
                  type="button"
                  className="ra-glyph"
                  aria-label={`${text("install")} ${name(app)}`}
                  disabled={busy || !canInstall(app)}
                  onClick={() => onInstall(app)}
                >
                  <AppIcon id={app.id} name={name(app)} />
                </button>
                <div className="ra-row-text">
                  <h3>{name(app)}</h3>
                  <p title={summary(app)}>{summary(app)}</p>
                  {app.warning && <small>{warning(app)}</small>}
                </div>
                {canInstall(app) ? (
                  <button type="button" className="ra-btn sm" disabled={busy} onClick={() => onInstall(app)}>
                    <Plus size={14} aria-hidden="true" />
                    {text("install")}
                  </button>
                ) : (
                  <span className="ra-chip">{limit(app)}</span>
                )}
              </article>
            ))}
          </div>
        </section>
      )}
      {!embedded && <section className="ra-info">
        <span className="ra-info-icon">
          <Bot size={20} aria-hidden="true" />
        </span>
        <div>
          <p className="lead">{text(info)}</p>
          <p>{text("retention")}</p>
        </div>
        <Link className="ra-link" to="/apps/workspaces">
          {text("legacy")}
          <ArrowUpRight size={14} aria-hidden="true" />
        </Link>
      </section>}
    </>
  );
}
