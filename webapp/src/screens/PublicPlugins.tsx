import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { publicPlugins, type PublicCatalog, type PublicPlugin } from "../domain/publicPlugins";
import "./publicPlugins.css";

export default function PublicPlugins({ view = "all", query = "" }: { view?: "all" | "installed" | "discover"; query?: string }) {
  const { i18n } = useTranslation();
  const zh = (i18n.resolvedLanguage || i18n.language).startsWith("zh");
  const tx = (a: string, b: string) => zh ? a : b;
  const [data, setData] = useState<PublicCatalog | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [confirm, setConfirm] = useState<{ plugin: PublicPlugin; action: string } | null>(null);
  const mounted = useRef(true);
  const operation = useRef(false);
  const navigate = useNavigate();
  const errorText = (code: string) => ({
    plugin_busy: tx("插件有排队或执行中的任务，请完成或取消任务后重试。", "Finish or cancel active jobs before changing this plugin."),
    plugin_signature_invalid: tx("发布签名校验失败，未安装。请联系发布者。", "Publisher signature verification failed."),
    plugin_integrity_failed: tx("插件文件校验失败，未启动。", "Plugin files failed verification."),
    plugin_start_failed: tx("插件启动失败；更新时会恢复原版本。", "Plugin failed to start; updates restore the previous version."),
    plugin_not_enabled: tx("请先安装并启用插件。", "Install and enable the plugin first."),
    plugin_no_update: tx("当前没有更高版本可更新。", "No newer version is available."),
  }[code] || tx("连接公开插件源失败，请检查网络后重试。", "Could not reach the public plugin source. Check your connection and retry."));
  useEffect(() => {
    mounted.current = true;
    publicPlugins.overview().then(value => { if (mounted.current) setData(value); }).catch(() => {
      if (mounted.current) setError(tx("当前 Ryn 未提供公开插件管理，请升级 Ryn。", "Upgrade Ryn to use public plugin management."));
    });
    return () => { mounted.current = false; };
  }, []); // Read cached catalog only. No automatic downloads or installations.
  const run = async (key: string, fn: () => Promise<PublicCatalog>) => {
    if (operation.current) return;
    operation.current = true; setBusy(key); setError("");
    try { const value = await fn(); if (mounted.current) setData(value); }
    catch (err) { if (mounted.current) setError(errorText(err instanceof Error ? err.message : "")); }
    finally { operation.current = false; if (mounted.current) setBusy(""); }
  };
  const items = (data?.plugins || []).filter(p => (view !== "installed" || p.installed_version)
    && (view !== "discover" || !p.installed_version)
    && (!query || `${p.name} ${p.description} ${p.id}`.toLowerCase().includes(query.toLowerCase())));
  return <section className="public-plugins" aria-label={tx("公开插件", "Public plugins")}>
    <div className="public-plugins-heading"><div><h2>{tx("公开插件", "Public plugins")}</h2>
      <p>{tx("从 Ryn 公开发布源发现和安装。Gshopper 等私有插件通过私有安装包导入，使用各自的私有更新源。", "Discover and install from Ryn's public source. Private plugins such as Gshopper use private installers and their own update sources.")}</p></div>
      <button disabled={!!busy} onClick={() => void run("refresh", publicPlugins.refresh)}>{busy === "refresh" ? tx("正在检查…", "Checking…") : tx("刷新目录 / 检查更新", "Refresh catalog / Check updates")}</button>
    </div>
    {data?.checked_at && <p>{tx("上次成功检查：", "Last successful check: ")}{new Date(data.checked_at * 1000).toLocaleString()}</p>}
    {(error || data?.error) && <p role="alert">{error || errorText(data!.error)}</p>}
    {data?.legacy_data && !data.plugins.some(p => p.id === "image-studio" && p.installed_version) && <p>{tx("检测到原有做图作品。安装“做图”后会继续使用这些数据；分享需重新开启。", "Existing image history is preserved. Install Image Studio to reuse it, then enable sharing if needed.")}</p>}
    {!items.length && <p>{tx("点击“刷新目录 / 检查更新”获取最新公开插件。已安装插件在“已安装”中查看。", "Refresh to discover public plugins. Installed plugins appear under Installed.")}</p>}
    <div className="public-plugin-grid">{items.map(p => <article className="public-plugin-card" key={p.id}>
      <h3>{p.name} <small>{tx("公开", "Public")}</small></h3><p>{p.description}</p>
      <p>{p.installed_version ? `${tx("已安装", "Installed")} ${p.installed_version} · ${p.enabled ? p.running ? tx("已启用", "Enabled") : tx("未运行", "Not running") : tx("已停用", "Disabled")}` : tx("未安装", "Not installed")} · {tx("最新版本", "Latest")} {p.version}</p>
      {p.update_available && <strong>{tx("有新版本可更新", "Update available")}</strong>}
      {p.error && <p role="alert">{errorText(p.error)}</p>}
      <details><summary>{tx("更新说明与来源", "Release notes and source")}</summary><p>{p.notes}</p><p>{tx("公开来源：老板的 rynmesh 仓库；更新不使用公司账号。", "Public source: the rynmesh repository; no company credentials are used.")}</p></details>
      <div className="public-plugin-actions">
        {!p.installed_version && <button disabled={!!busy} onClick={() => setConfirm({ plugin: p, action: "install" })}>{tx("安装", "Install")}</button>}
        {p.running && <button disabled={!!busy} onClick={() => navigate(`/apps/public-plugins/${encodeURIComponent(p.id)}`)}>{tx("打开", "Open")}</button>}
        {p.installed_version && <button disabled={!!busy} onClick={() => void run(p.id, () => publicPlugins.action(p.id, p.enabled ? "disable" : "enable"))}>{p.enabled ? tx("停用", "Disable") : tx("启用", "Enable")}</button>}
        {p.update_available && <button disabled={!!busy} onClick={() => setConfirm({ plugin: p, action: "update" })}>{tx("更新", "Update")}</button>}
        {p.rollback_available && <button disabled={!!busy} onClick={() => setConfirm({ plugin: p, action: "rollback" })}>{tx("回退上一版", "Roll back")}</button>}
        {p.installed_version && <button disabled={!!busy} onClick={() => setConfirm({ plugin: p, action: "uninstall" })}>{tx("卸载", "Uninstall")}</button>}
        {p.running && p.id === "image-studio" && <Link to="/apps/sites/image-studio">{tx("管理任务与分享", "Manage jobs and sharing")}</Link>}
      </div>{busy === p.id && <p role="status">{tx("正在处理，请稍候…", "Working…")}</p>}
    </article>)}</div>
    {confirm && <div className="public-plugin-confirm" role="dialog" aria-modal="true" aria-label={tx("确认插件操作", "Confirm plugin action")}>
      <h3>{confirm.plugin.name} · {confirm.action === "uninstall" ? tx("卸载", "Uninstall") : confirm.action === "rollback" ? tx("回退上一版", "Roll back") : `${tx("安装版本", "Install version")} ${confirm.plugin.version}`}</h3>
      <p>{confirm.action === "uninstall" ? tx("卸载会关闭本机插件和对外分享。作品和配置保留，重新安装后可继续使用。", "Uninstall stops the plugin and sharing. Images and settings remain available for reinstall.") : tx("此插件会在本机运行发布者的代码，调用本机登录的 Codex 并保存图片。安装和更新不会自动开启对外分享；已有分享设置在更新时保留。", "This plugin runs publisher code locally, uses your signed-in Codex and stores images. Installation does not enable sharing; updates preserve existing sharing settings.")}</p>
      <button onClick={() => setConfirm(null)}>{tx("取消", "Cancel")}</button>
      <button onClick={() => { const selected = confirm; setConfirm(null); void run(selected.plugin.id, () => publicPlugins.action(selected.plugin.id, selected.action)); }}>{tx("确认", "Confirm")}</button>
    </div>}
  </section>;
}
