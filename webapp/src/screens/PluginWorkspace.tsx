import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Box, ExternalLink, MessageCircle, Pin, PinOff, RefreshCw, Settings, Table2 } from "lucide-react";
import { agentExtensions, type ExtensionOverview } from "../domain/agentExtensions";
import { PeerProviderSettings } from "./PeerProviderSettings";
import { PluginViews } from "./PluginViews";
import { isPluginWorkspacePinned, PIN_CHANGE_EVENT, pluginWorkspacePath, setPluginWorkspacePinned } from "./pluginWorkspaceNavigation";
import "./PluginWorkspace.css";

export default function PluginWorkspace() {
  const { pluginId = "" } = useParams();
  // A route change cannot retain another plugin's results or provider state.
  return <Workspace key={pluginId} pluginId={pluginId} />;
}

function Workspace({ pluginId }: { pluginId: string }) {
  const { i18n } = useTranslation();
  const zh = (i18n.resolvedLanguage || i18n.language).startsWith("zh");
  const tx = (chinese: string, english: string) => zh ? chinese : english;
  const location = useLocation(), navigate = useNavigate();
  const query = new URLSearchParams(location.search);
  const [overview, setOverview] = useState<ExtensionOverview | null>(null);
  const [loading, setLoading] = useState(true), [failed, setFailed] = useState(false);
  const [pinned, setPinned] = useState(() => isPluginWorkspacePinned(pluginId));
  const [tab, setTab] = useState(query.get("tab") === "peers" ? "peers" : "pages");
  const generation = useRef(0);
  const reload = useCallback(async () => {
    const current = ++generation.current;
    setLoading(true); setFailed(false);
    try {
      const next = await agentExtensions.overview();
      if (current === generation.current) setOverview(next);
    } catch { if (current === generation.current) setFailed(true); }
    finally { if (current === generation.current) setLoading(false); }
  }, []);
  useEffect(() => { void reload(); return () => { generation.current++; }; }, [reload]);
  useEffect(() => {
    const sync = () => setPinned(isPluginWorkspacePinned(pluginId));
    window.addEventListener(PIN_CHANGE_EVENT, sync); window.addEventListener("storage", sync);
    return () => { window.removeEventListener(PIN_CHANGE_EVENT, sync); window.removeEventListener("storage", sync); };
  }, [pluginId]);

  const plugin = overview?.plugins.find(item => item.id === pluginId);
  if (!plugin) return <section className="pw-workspace">
    <h1>{tx("插件工作台", "Plugin workspace")}</h1>
    <p role={failed ? "alert" : "status"}>{loading ? tx("正在加载插件…", "Loading plugin…") : failed
      ? tx("无法读取插件，请重试。", "Could not load the plugin. Retry.")
      : tx("此插件未安装。请先导入插件包。", "This plugin is not installed. Import its package first.")}</p>
    <button className="ae-btn" disabled={loading} onClick={() => void reload()}>{tx("重试", "Retry")}</button>
    <Link className="ae-btn" to="/settings/extensions/plugins">{tx("管理插件", "Manage plugins")}</Link>
  </section>;

  const servers = [...new Map([
    ...(plugin.mcp_servers || []),
    ...(overview?.mcp_servers || []).filter(item => item.plugin_id === plugin.id || plugin.mcp_server_ids.includes(item.id)),
  ].map(item => [item.id, item])).values()];
  const hasPages = Boolean(plugin.views?.length), hasProvider = Boolean(plugin.peer_provider);
  const showProvider = hasProvider && (tab === "peers" || !hasPages);
  const path = pluginWorkspacePath(plugin.id);
  const settings = `/settings/extensions/plugins?plugin=${encodeURIComponent(plugin.id)}`;
  return <div className="pw-workspace">
    <header className="pw-header">
      <div><Box size={25} aria-hidden="true" /><h1>{plugin.title}</h1>{plugin.version && <span>{plugin.version}</span>}</div>
      <p>{plugin.description}</p>
      <div className="pw-actions">
        <button className="ae-btn" onClick={() => { setPluginWorkspacePinned({ id: plugin.id, title: plugin.title }, !pinned); setPinned(!pinned); }}>
          {pinned ? <PinOff size={15} /> : <Pin size={15} />}{pinned ? tx("取消固定", "Unpin from sidebar") : tx("固定到侧栏", "Pin to sidebar")}
        </button>
        <button className="ae-btn" disabled={loading} onClick={() => void reload()}><RefreshCw size={15} />{tx("刷新", "Refresh")}</button>
        <Link className="ae-btn" to={settings}><Settings size={15} />{tx("插件设置", "Plugin settings")}</Link>
        {query.get("standalone") === "1" ? <Link className="ae-btn" to={path}>{tx("返回主界面", "Back to main window")}</Link> :
          <a className="ae-btn" href={`${path}${location.search ? location.search + "&" : "?"}standalone=1`} target="_blank" rel="noopener noreferrer"><ExternalLink size={15} />{tx("独立窗口", "Open in new window")}</a>}
      </div>
    </header>
    {!plugin.enabled && <p role="status">{tx("插件已停用。请在设置中启用。", "This plugin is disabled. Enable it in settings.")}</p>}
    {failed && <p role="alert">{tx("刷新失败，当前信息可能已过期。请重试。", "Refresh failed. The displayed information may be stale. Retry.")}</p>}
    <nav className="pw-tabs" aria-label={tx("插件功能", "Plugin features")}>
      {hasPages && <button className="ae-btn" aria-pressed={!showProvider} onClick={() => setTab("pages")}><Table2 size={16} />{tx(`页面 (${plugin.views!.length})`, `Pages (${plugin.views!.length})`)}</button>}
      {hasProvider && <button className="ae-btn" aria-pressed={showProvider} onClick={() => setTab("peers")}><MessageCircle size={16} />{tx("成员与模型", "Members & models")}</button>}
    </nav>
    {!showProvider && hasPages && <PluginViews key={plugin.instance_id || plugin.id} plugin={plugin} servers={servers}
      initialViewId={query.get("view") || undefined} onRefresh={() => void reload()}
      onOpenServer={id => navigate(`/settings/extensions/mcp?server=${encodeURIComponent(id)}`)} />}
    {showProvider && <>
      <PeerProviderSettings key={plugin.instance_id || plugin.id} plugin={plugin} />
      <section className="ae-section-card" aria-label={tx("成员消息", "Member messages")}>
        <h3>{tx("成员消息", "Member messages")}</h3>
        <p>{tx("在 Device Drop 中选择此提供方的设备发送加密文字。成员资格不会授予文件传输或设备控制权限。", "Select a device from this provider in Device Drop to send encrypted text. Membership does not grant file transfer or device control.")}</p>
        <Link className="ae-btn" to="/devices">{tx("打开 Device Drop", "Open Device Drop")}</Link>
      </section>
    </>}
    {!hasPages && !hasProvider && <p>{tx("此插件未声明页面或节点提供方，请在插件设置中查看其工具与技能。", "This plugin declares no pages or peer provider. View its tools and skills in plugin settings.")}</p>}
  </div>;
}
