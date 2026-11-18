import { useEffect, useState } from "react";
import { Navigate, useLocation, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { publicPlugins } from "../domain/publicPlugins";

export default function PublicPluginPage() {
  const { pluginId = "" } = useParams();
  const { search } = useLocation();
  return <Navigate replace to={`/apps/app/public/${encodeURIComponent(pluginId)}${search}`} />;
}

export function LegacyImageStudioRedirect() {
  return <Navigate replace to="/apps/app/public/image-studio?tab=share" />;
}

export function PublicPluginFrame({ pluginId }: { pluginId: string }) {
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
  if (error) return <p className="ns-error" role="alert">{zh ? "无法打开，请确认应用已安装并启用。" : "Install and enable the app before opening it."}</p>;
  if (!page) return <p className="ns-muted" role="status">{zh ? "正在打开应用…" : "Opening app…"}</p>;
  return <iframe className="ns-card" title={page.name} src={page.url} referrerPolicy="no-referrer"
    sandbox="allow-scripts allow-forms allow-same-origin allow-downloads" style={{ width: "100%", height: "75vh" }} />;
}
