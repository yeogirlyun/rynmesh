import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { publicPlugins } from "../domain/publicPlugins";

export default function PublicPluginPage() {
  const { pluginId = "" } = useParams();
  const { i18n } = useTranslation();
  const zh = (i18n.resolvedLanguage || i18n.language).startsWith("zh");
  const [page, setPage] = useState<{ url: string; name: string } | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    let active = true;
    setPage(null); setError(false);
    publicPlugins.open(pluginId).then(value => {
      const url = new URL(value.url);
      if (url.protocol !== "http:" || !/^[0-9a-f]{32}\.site\.localhost$/.test(url.hostname) || !url.port || url.username || url.password) throw new Error("invalid_origin");
      if (active) setPage(value);
    }).catch(() => { if (active) setError(true); });
    return () => { active = false; };
  }, [pluginId]);
  return <section className="ns-page"><header><Link to="/settings/plugins">{zh ? "返回插件" : "Back to plugins"}</Link><h1>{page?.name || (zh ? "打开插件" : "Open plugin")}</h1><p>{zh ? "在我的电脑上运行" : "Running on my computer"}</p></header>
    {error && <p role="alert">{zh ? "无法打开，请在插件页确认已安装并启用。" : "Install and enable the plugin before opening it."}</p>}
    {page && <iframe title={page.name} src={page.url} referrerPolicy="no-referrer" sandbox="allow-scripts allow-forms allow-same-origin allow-downloads" style={{ width: "100%", height: "75vh", border: 0 }} />}
  </section>;
}
