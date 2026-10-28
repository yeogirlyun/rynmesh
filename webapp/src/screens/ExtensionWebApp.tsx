import { useEffect, useRef, useState } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import { useAppContext } from '../appContext';
import { agentExtensions, queryPluginTool, type ExtensionPlugin } from '../domain/agentExtensions';
import { peerProviders } from '../domain/peerProviders';

/** Remote UI stays on its own origin; only declared capabilities cross the frame boundary. */
export default function ExtensionWebApp() {
  const { pluginId = '' } = useParams();
  const { client } = useAppContext();
  const navigate = useNavigate();
  const [plugin, setPlugin] = useState<ExtensionPlugin>();
  const [error, setError] = useState('');
  const frame = useRef<HTMLIFrameElement>(null);
  useEffect(() => {
    let active = true;
    agentExtensions.overview().then(o => {
      const p = o.plugins.find(p => p.id === pluginId);
      if (active) { setPlugin(p); if (!p?.enabled || !p.web_app) setError('Plugin unavailable'); }
    }).catch(() => active && setError('Plugin unavailable'));
    return () => { active = false; };
  }, [pluginId]);
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
  if (error) return <p role="alert">{error} · <Link to="/settings/extensions/plugins">Manage plugins</Link></p>;
  if (!plugin?.web_app) return <p>Loading…</p>;
  const url = new URL(plugin.web_app.url); if (url.origin === window.location.origin) return <p role="alert">Plugin app must use a separate origin</p>; url.searchParams.set('host_origin', window.location.origin);
  return <iframe ref={frame} title={plugin.title} src={url.href} sandbox="allow-scripts allow-forms allow-same-origin allow-popups" referrerPolicy="no-referrer" style={{ border: 0, width: '100%', height: 'calc(100vh - 48px)' }} />;
}
