import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { publicPlugins, type PublicCatalog, type PublicPlugin } from "../domain/publicPlugins";
import "./publicPlugins.css";
import "./apps.css";

export function usePublicCatalog() {
  const [data, setData] = useState<PublicCatalog | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [unavailable, setUnavailable] = useState(false);
  const mounted = useRef(true);
  const operation = useRef(false);
  useEffect(() => {
    let active = true;
    mounted.current = true;
    publicPlugins.overview().then(value => { if (active) setData(value); }).catch(() => {
      if (active) setUnavailable(true);
    });
    return () => { active = false; mounted.current = false; };
  }, []); // Read cached catalog only. No automatic downloads or installations.
  const run = async (key: string, fn: () => Promise<PublicCatalog>) => {
    if (operation.current) return;
    operation.current = true; setBusy(key); setError("");
    try {
      const value = await fn();
      if (mounted.current) { setData(value); setUnavailable(false); }
    } catch (err) {
      if (mounted.current) setError(err instanceof Error ? err.message : "plugin_download_failed");
    }
    finally { operation.current = false; if (mounted.current) setBusy(""); }
  };
  return { data, busy, error, unavailable, run };
}

export default function PublicPlugins({ catalog }: { catalog: ReturnType<typeof usePublicCatalog> }) {
  const { i18n } = useTranslation();
  const zh = (i18n.resolvedLanguage || i18n.language).startsWith("zh");
  const tx = (a: string, b: string) => zh ? a : b;
  const { data, busy, error, unavailable, run } = catalog;
  const [confirm, setConfirm] = useState<{ plugin: PublicPlugin; action: "install" | "update" } | null>(null);
  const errorText = (code: string) => ({
    plugin_busy: tx("插件有排队或执行中的任务，请完成或取消任务后重试。", "Finish or cancel active jobs before changing this plugin."),
    plugin_signature_invalid: tx("发布签名校验失败，未安装。请联系发布者。", "Publisher signature verification failed."),
    plugin_integrity_failed: tx("插件文件校验失败，未启动。", "Plugin files failed verification."),
    plugin_start_failed: tx("插件启动失败；更新时会恢复原版本。", "Plugin failed to start; updates restore the previous version."),
    plugin_not_enabled: tx("请先安装并启用插件。", "Install and enable the plugin first."),
    plugin_no_update: tx("当前没有更高版本可更新。", "No newer version is available."),
  }[code] || tx("连接公开插件源失败，请检查网络后重试。", "Could not reach the public plugin source. Check your connection and retry."));
  const items = data?.plugins ?? [];
  const currentError = error || data?.error;
  return <section aria-label={tx("公开应用", "Public apps")}>
    <div className="apps-hub-section-heading">
      <div><h2 style={{ fontSize: 16 }}>{tx("公开应用", "Public apps")}</h2>
        <p className="apps-hub-muted">{tx("来自 Ryn 公开目录，安装前校验签名", "From the Ryn public catalog, with signatures verified before installation")}</p></div>
      <div className="apps-hub-actions">
        {data?.checked_at && <p className="apps-hub-muted">{tx("上次检查", "Last checked")} {new Date(data.checked_at * 1000).toLocaleString(zh ? "zh-CN" : "en")}</p>}
        <button className="apps-hub-button" disabled={!!busy || (!data && !unavailable)} onClick={() => void run("refresh", publicPlugins.refresh)}>
          {busy === "refresh" ? tx("正在检查…", "Checking…") : tx("刷新", "Refresh")}
        </button>
      </div>
    </div>
    {currentError ? <p role="alert">{errorText(currentError)}</p> : unavailable &&
      <p role="alert">{tx("当前 Ryn 未提供公开插件管理，请升级 Ryn。", "Upgrade Ryn to use public plugin management.")}</p>}
    {data?.legacy_data && !data.plugins.some(p => p.id === "image-studio" && p.installed_version) && <p>{tx("检测到原有做图作品。安装“做图”后会继续使用这些数据；分享需重新开启。", "Existing image history is preserved. Install Image Studio to reuse it, then enable sharing if needed.")}</p>}
    {data && !items.length && <p className="apps-hub-muted">{tx("点击刷新，获取最新公开应用。", "Refresh to discover public apps.")}</p>}
    <div className="apps-hub-grid">{items.map(plugin => <article className="apps-hub-card" key={plugin.id}
      style={plugin.update_available ? { borderColor: "var(--accent-amber-border)" } : undefined}>
      <div className="apps-hub-card-heading">
        <span className="apps-hub-icon" aria-hidden="true">{Array.from(plugin.name)[0]}</span>
        <div className="apps-hub-card-text">
          <h3>{plugin.name}</h3>
          <p className="apps-hub-muted">{plugin.installed_version
            ? `${tx("已安装", "Installed")} ${plugin.installed_version} · ${plugin.update_available ? `${tx("最新版本", "Latest")} ${plugin.version}` : tx("已是最新", "Up to date")}`
            : `${tx("未安装", "Not installed")} · ${plugin.version}`}</p>
        </div>
      </div>
      <p className="apps-hub-muted">{plugin.description}</p>
      {plugin.error && <p role="alert">{errorText(plugin.error)}</p>}
      <div className="apps-hub-card-actions">
        {!plugin.installed_version && <button className="apps-hub-button" disabled={!!busy} onClick={() => setConfirm({ plugin, action: "install" })}>
          {tx("安装到本机", "Install locally")}
        </button>}
        {plugin.update_available && <button className="apps-hub-button" disabled={!!busy} onClick={() => setConfirm({ plugin, action: "update" })}
          style={{ background: "var(--accent-blue)", color: "var(--bg-primary)" }}>{tx("更新到", "Update to")} {plugin.version}</button>}
        {plugin.installed_version && <Link className="apps-hub-button" to={`/apps/app/public/${encodeURIComponent(plugin.id)}`}>{tx("打开", "Open")}</Link>}
      </div>
      {busy === plugin.id && <p role="status">{tx("正在处理，请稍候…", "Working…")}</p>}
    </article>)}</div>
    {confirm && <div className="public-plugin-confirm" role="dialog" aria-modal="true" aria-label={tx("确认插件操作", "Confirm plugin action")}>
      <h3>{confirm.plugin.name} · {confirm.action === "update" ? tx("更新到", "Update to") : tx("安装版本", "Install version")} {confirm.plugin.version}</h3>
      <p>{confirm.plugin.id === "image-studio"
        ? tx("此插件会在本机运行发布者的代码，调用本机登录的 Codex 并保存图片。安装和更新不会自动开启对外分享；已有分享设置在更新时保留。", "This plugin runs publisher code locally, uses your signed-in Codex and stores images. Installation does not enable sharing; updates preserve existing sharing settings.")
        : tx("此应用会在本机运行发布者的代码。你可以在应用详情里管理共享。", "This app runs publisher code on this computer. You can manage sharing from the app details.")}</p>
      <button className="apps-hub-button" onClick={() => setConfirm(null)}>{tx("取消", "Cancel")}</button>
      <button className="apps-hub-button" onClick={() => { const selected = confirm; setConfirm(null); void run(selected.plugin.id, () => publicPlugins.action(selected.plugin.id, selected.action)); }}>{tx("确认", "Confirm")}</button>
    </div>}
  </section>;
}
