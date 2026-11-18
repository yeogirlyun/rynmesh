import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import CraftApps from "./CraftApps";
import { PublicInstalledCount } from "./CraftPluginLibrary";
import PublicPlugins, { usePublicCatalog } from "./PublicPlugins";

export default function AppsDiscover() {
  const { i18n } = useTranslation();
  const zh = (i18n.resolvedLanguage || i18n.language).startsWith("zh");
  const catalog = usePublicCatalog();
  const installed = (catalog.data?.plugins ?? []).filter(app => app.installed_version);
  const updates = installed.filter(app => app.update_available);
  const first = updates[0];
  const releases = updates.map(app => `${app.name} ${app.version}`).join(zh ? "、" : ", ");

  return <>
    <style>{".pf-shell .apps-hub > .craft-center.craft-manager { width: 100%; max-width: none; padding: 0; margin: 0; }"}</style>
    {first && <aside className="apps-hub-section-heading" aria-label={zh ? "应用更新" : "App updates"}
      style={{ margin: 0, padding: "12px 16px", borderRadius: 12, background: "var(--accent-amber-soft)" }}>
      <span>{zh ? `${updates.length} 个应用有新版本：${releases}`
        : `${updates.length} ${updates.length === 1 ? "app has" : "apps have"} updates: ${releases}`}</span>
      <Link className="apps-hub-button" to={`/apps/app/public/${encodeURIComponent(first.id)}?tab=version`}>
        {zh ? "去更新" : "Review updates"}
      </Link>
    </aside>}
    <PublicPlugins catalog={catalog} />
    <PublicInstalledCount.Provider value={installed.length}>
      <CraftApps manage />
    </PublicInstalledCount.Provider>
  </>;
}
