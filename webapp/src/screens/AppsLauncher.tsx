import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowRight, ArrowUpRight, Bot, Check, Info, Plus } from "lucide-react";
import type { CraftCatalog, CraftPlugin } from "../domain/craftPlugins";
import { tr } from "../uiI18n";
import AppIcon, { appTone } from "./appIcons";
import "./craft-apps-refresh.css";

const groups = ["office", "workflow", "design", "media", "storage"] as const;
export type AppsFilter = "all" | (typeof groups)[number];
const isBuiltin = (app: CraftPlugin) => app.kind === "builtin_adapter";
const canInstall = (app: CraftPlugin) => app.installable ?? app.web;

type Props = {
  catalog: CraftCatalog;
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
export default function AppsLauncher({ catalog, busy, filter, onFilterChange, text, onOpen, onInstall, onAgent }: Props) {
  const { t, i18n } = useTranslation();
  const name = (app: CraftPlugin) => text(app.id);
  const warning = (app: CraftPlugin) => text(app.warning === "cjk_font_coverage" ? "cjkWarningShort" : "fontWarningShort");
  const summary = (app: CraftPlugin) => text(`descriptions.${app.id}`, { defaultValue: app.name });
  // Why an app cannot be started or installed here, shown as a label rather than replacing its description.
  const limit = (app: CraftPlugin) => (canInstall(app) ? "" : text(isBuiltin(app) ? "unavailable" : "native"));
  const visible = catalog.plugins.filter((app) => filter === "all" || app.group === filter);
  const installed = visible.filter((app) => app.installed);
  const notInstalled = visible.filter((app) => !app.installed);
  const count = (id: AppsFilter) => catalog.plugins.filter((app) => id === "all" || app.group === id).length;
  const installedTotal = catalog.plugins.filter((app) => app.installed).length;
  // Office apps have their own Agent note when this build carries it; otherwise the general one.
  const info =
    (filter === "all" || filter === "office") && i18n.exists("craft.officeAgentInfo") ? "officeAgentInfo" : "enableInfo";
  return (
    <>
      <header className="ra-head">
        <div>
          <h1>{text("title")}</h1>
          <p>{text("subtitle")}</p>
          <div className="ra-stats">
            <span className="ra-chip ok">
              <Check size={13} aria-hidden="true" />
              {tr("{{count}} installed", { count: installedTotal })}
            </span>
            <span className="ra-chip info">{tr("{{count}} not installed", { count: catalog.plugins.length - installedTotal })}</span>
          </div>
        </div>
        <Link className="ra-btn" to="/settings/plugins">
          {text("manage")}
          <ArrowUpRight size={15} aria-hidden="true" />
        </Link>
      </header>
      <nav className="ra-filters" aria-label={text("title")}>
        {(["all", ...groups] as const).map((id) => (
          <button key={id} type="button" aria-pressed={filter === id} className={filter === id ? "on" : ""} onClick={() => onFilterChange(id)}>
            {id === "all" ? t("personal.all") : text(id)}
            <em aria-hidden="true">{count(id)}</em>
          </button>
        ))}
      </nav>
      {installed.length > 0 && (
        <section className="ra-section" aria-label={text("installed")}>
          <h2>
            {text("installed")} <span>({installed.length})</span>
          </h2>
          <div className="ra-grid4">
            {installed.map((app) => (
              <article className={`ra-card ra-t-${appTone(app.id)}${app.enabled ? "" : " off"}`} key={app.id} data-tool={app.id}>
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
                  {(limit(app) || !app.enabled) && <span className="ra-chip">{limit(app) || text("disabled")}</span>}
                </div>
                <div>
                  <h3>{name(app)}</h3>
                  <p>{summary(app)}</p>
                </div>
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
      <section className="ra-info">
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
      </section>
    </>
  );
}
