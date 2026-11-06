import { tr } from "../uiI18n";
import { useEffect, useRef, useState } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import { useAppContext } from '../appContext';
import { agentExtensions, queryPluginTool, type ExtensionPlugin } from '../domain/agentExtensions';
import { peerProviders } from '../domain/peerProviders';
import { useTranslation } from 'react-i18next';
import { invoke } from '@tauri-apps/api/core';
import { isTauriDesktop } from '../domain/nodeUrl';
import type { NodeClient } from '../domain/nodeClient';

/** Remote UI stays on its own origin; only declared capabilities cross the frame boundary. */
export default function ExtensionWebApp({ resolvedPlugin }: { resolvedPlugin?: ExtensionPlugin } = {}) {
  const { pluginId = '' } = useParams();
  const { client } = useAppContext();
  return <PluginWebApp key={pluginId} pluginId={pluginId} resolvedPlugin={resolvedPlugin} client={client} />;
}

/** Message telling a kept-alive plugin page whether the user can see it, so it can pause background work. */
export const PLUGIN_VISIBILITY_MESSAGE = 'ryn.plugin.visibility';

export function PluginWebApp({ pluginId, resolvedPlugin, client, visible = true }: {
  pluginId: string; resolvedPlugin?: ExtensionPlugin; client: NodeClient; visible?: boolean;
}) {
  const { i18n } = useTranslation();
  const zh = (i18n.resolvedLanguage || i18n.language).startsWith('zh');
  const tx = (chinese: string, english: string) => zh ? chinese : english;
  const navigate = useNavigate();
  const [plugin, setPlugin] = useState<ExtensionPlugin | undefined>(resolvedPlugin);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [reachable, setReachable] = useState(false);
  const frame = useRef<HTMLIFrameElement>(null);
  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  const announceVisibility = () => {
    const target = frame.current?.contentWindow;
    if (!target || !plugin?.web_app) return;
    try { target.postMessage({ type: PLUGIN_VISIBILITY_MESSAGE, visible: visibleRef.current }, new URL(plugin.web_app.url).origin); } catch { /* The frame is navigating. */ }
  };
  useEffect(announceVisibility, [visible]);
  // Keep an already-open error page in sync with supervisor recovery.
  useEffect(() => {
    if (!['plugin_service_unhealthy', 'plugin_service_start_timeout', 'plugin_service_exited', 'connection'].includes(error)) return;
    const timer = window.setTimeout(() => {
      setError(''); setReachable(false); setAttempt(value => value + 1);
    }, 10000);
    return () => window.clearTimeout(timer);
  }, [error, attempt]);
  useEffect(() => {
    let active = true;
    if (resolvedPlugin) {
      setPlugin(resolvedPlugin);
      if (!resolvedPlugin.enabled || !resolvedPlugin.web_app) setError('unavailable');
      return;
    }
    setError('');
    agentExtensions.overview().then(o => {
      const p = o.plugins.find(p => p.id === pluginId);
      if (active) { setPlugin(p); if (!p?.enabled || !p.web_app) setError('unavailable'); }
    }).catch(() => active && setError('unavailable'));
    return () => { active = false; };
  }, [pluginId, resolvedPlugin, attempt]);
  useEffect(() => {
    if (!plugin?.enabled || !plugin.web_app) return;
    let url: URL;
    try { url = new URL(plugin.web_app.url); } catch { setError('unavailable'); return; }
    if (url.origin === window.location.origin) { setError('origin'); return; }
    // WebView2 can display a blank error document and still fire iframe.onload.
    // Check loopback availability before mounting an app whose local service may be stopped.
    if (!['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) { setReachable(true); return; }
    let active = true;
    const controller = new AbortController();
    let timeout: number | undefined;
    setReachable(false);
    (async () => {
      if (isTauriDesktop()) {
        const managed = await invoke<boolean>('ensure_plugin_service', { pluginId });
        if (!active || managed) return;
      }
      if (!active) return;
      timeout = window.setTimeout(() => controller.abort(), 10000);
      await fetch(url.href, { method: 'HEAD', mode: 'no-cors', credentials: 'omit', cache: 'no-store', signal: controller.signal });
    })()
      .then(() => { if (active) setReachable(true); })
      .catch((reason) => { if (active) setError(typeof reason === 'string' && reason.startsWith('plugin_service_') ? reason : 'connection'); })
      .finally(() => window.clearTimeout(timeout));
    return () => { active = false; controller.abort(); window.clearTimeout(timeout); };
  }, [plugin, pluginId, attempt]);
  useEffect(() => {
    if (!plugin?.enabled || !plugin.web_app) return;
    const origin = new URL(plugin.web_app.url).origin;
    const onMessage = async (event: MessageEvent) => {
      if (event.source !== frame.current?.contentWindow || event.origin !== origin || event.data?.type !== 'ryn.plugin.request') return;
      const { id, method, args = [] } = event.data;
      if (typeof id !== 'string' || id.length > 100 || !Array.isArray(args) || args.length > 4) return;
      try {
        const live = (await agentExtensions.overview()).plugins.find(p => p.id === pluginId);
        if (!live?.enabled || live.web_app?.url !== plugin.web_app?.url) throw new Error();
        const permissions = live.web_app?.permissions || [];
        if (JSON.stringify(args).length > 65536) throw new Error();
        const methods: Record<string, { permission: string; run: () => unknown }> = {
          'agent.draft': { permission: 'agent', run: () => {
            if (typeof args[0] !== 'string' || !args[0].trim() || args[0].length > 16000) throw new Error();
            navigate('/agent', { state: { externalAppDraft: { id: pluginId, prompt: args[0] } } });
            return { opened: true, sent: false };
          } },
          'models.list': { permission: 'models', run: () => client.getCLIServices() },
          'models.access': { permission: 'models', run: async () => {
            const value = await client.getInferenceAccess();
            return { models: value.models, targets: value.targets, aliases: value.aliases, keys: [] };
          } },
          'models.share': { permission: 'models', run: () => client.setCLISharing(args[0], args[1]) },
          'messages.list': { permission: 'messages', run: () => client.listMessages(args[0]) },
          'messages.send': { permission: 'messages', run: () => client.sendMessage(args[0]) },
          'peer.status': { permission: 'peer_provider', run: () => peerProviders.status(pluginId) },
          'peer.sync': { permission: 'peer_provider', run: () => peerProviders.autoSync(pluginId) },
          'peer.bind': { permission: 'peer_provider', run: () => peerProviders.bind(pluginId) },
          'peer.refresh': { permission: 'peer_provider', run: () => peerProviders.refresh(pluginId) },
          'peer.workspace': { permission: 'peer_provider', run: () => peerProviders.workspace(pluginId) },
          'peer.catalog': { permission: 'peer_provider', run: () => peerProviders.catalog(pluginId) },
          'peer.models': { permission: 'peer_provider', run: () => peerProviders.models(pluginId) },
          'peer.publish': { permission: 'peer_provider', run: () => peerProviders.publish(pluginId, args[0]) },
          'tools.query': { permission: 'tools', run: () => {
            if (typeof args[0] !== 'string' || !args[1] || typeof args[1] !== 'object' || Array.isArray(args[1])) throw new Error();
            return queryPluginTool(live, args[0], args[1]);
          } },
        };
        const operation = methods[method];
        if (!operation || !permissions.includes(operation.permission)) throw new Error();
        const result = await operation.run();
        frame.current?.contentWindow?.postMessage({ type: 'ryn.plugin.response', id, result }, origin);
      } catch {
        frame.current?.contentWindow?.postMessage({ type: 'ryn.plugin.response', id, error: 'plugin_capability_unavailable' }, origin);
      }
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [plugin, pluginId, client, navigate]);
  if (error) return <section style={{ padding: 24 }}>
    <p role="alert">{error === 'connection'
      ? tx('无法连接插件服务，请重试。', 'Could not connect to the plugin service. Please retry.')
      : error === 'plugin_service_port_in_use' ? tx('插件端口被其他进程占用。请退出旧版插件服务后重试。', 'Another process is using the plugin port. Close the previous plugin service and retry.')
      : error === 'plugin_service_unhealthy' ? tx('插件服务暂时无响应，正在等待自动恢复…', 'The plugin service is not responding. Waiting for automatic recovery…')
      : error === 'plugin_service_files_missing' ? tx('插件运行文件缺失，请重新安装此插件的服务包。', 'Plugin runtime files are missing. Reinstall its service package.')
      : error.startsWith('plugin_service_') ? tx('插件服务启动失败，请重试。错误代码：', 'The plugin service could not start. Retry. Error: ') + error
      : error === 'origin' ? tr('Plugin app must use a separate origin') : tx('插件不可用。请检查插件是否已启用。', 'Plugin unavailable. Check that it is enabled.')}</p>
    <button className="ae-btn" onClick={() => { setError(''); setReachable(false); setAttempt(value => value + 1); }}>{tx('重试', 'Retry')}</button>
    {' '}<Link to="/settings/extensions/plugins">{tr('Manage plugins')}</Link>
  </section>;
  if (!plugin?.enabled || !plugin.web_app || !reachable) return <div role="status" aria-busy="true" style={{ padding: 24 }}>{tx('正在连接插件服务…', 'Connecting to the plugin service…')}</div>;
  const url = new URL(plugin.web_app.url); url.searchParams.set('host_origin', window.location.origin);
  const sandbox = ['allow-scripts', 'allow-forms', 'allow-same-origin', 'allow-popups'];
  if (plugin.web_app.permissions.includes('downloads')) sandbox.push('allow-downloads');
  if (plugin.web_app.permissions.includes('dialogs')) sandbox.push('allow-modals');
  return <iframe ref={frame} title={plugin.title} src={url.href} sandbox={sandbox.join(' ')} referrerPolicy="no-referrer" onLoad={announceVisibility} onError={() => setError('connection')} style={{ border: 0, width: '100%', height: '100%', flex: 1, minHeight: 0 }} />;
}
