import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ExtensionPlugin } from "../domain/agentExtensions";
import { peerProviders, type PeerProviderDirectory, type PeerProviderModel, type PeerProviderStatus } from "../domain/peerProviders";

export function PeerProviderSettings({ plugin }: { plugin: ExtensionPlugin }) {
  const { i18n } = useTranslation();
  const tx = (zh: string, en: string) => i18n.language.startsWith("zh") ? zh : en;
  const [status, setStatus] = useState<PeerProviderStatus | null>(null);
  const [directory, setDirectory] = useState<PeerProviderDirectory | null>(null);
  const [models, setModels] = useState<PeerProviderModel[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [browserUrl, setBrowserUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [notice, setNotice] = useState("");
  const generation = useRef(0);

  useEffect(() => {
    const current = ++generation.current;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    setStatus(null); setDirectory(null); setModels([]); setSelected([]); setBrowserUrl(""); setError(false); setNotice(""); setBusy(false);
    if (!plugin.enabled) return () => { generation.current++; abort.abort(); };
    const poll = async () => {
      try {
        const next = await peerProviders.status(plugin.id, abort.signal);
        if (generation.current !== current) return;
        setStatus(next);
        if (next.status !== "pending") {
          setSelected(next.selected_service_ids);
          if (next.status === "connected") setBrowserUrl("");
          return;
        }
        timer = setTimeout(() => void poll(), 1500);
      } catch { if (generation.current === current && !abort.signal.aborted) setError(true); }
    };
    void poll();
    return () => { generation.current++; abort.abort(); clearTimeout(timer); };
  }, [plugin.id, plugin.instance_id, plugin.enabled]);

  useEffect(() => {
    if (status?.status !== "pending" || !browserUrl) return;
    const current = generation.current;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await peerProviders.status(plugin.id);
        if (disposed || generation.current !== current) return;
        setStatus(next);
        if (next.status === "pending") timer = setTimeout(() => void poll(), 1500);
        else { setBrowserUrl(""); setSelected(next.selected_service_ids); }
      } catch { if (!disposed && generation.current === current) setError(true); }
    };
    timer = setTimeout(() => void poll(), 1500);
    return () => { disposed = true; clearTimeout(timer); };
  }, [browserUrl, plugin.id, status?.status]);

  useEffect(() => {
    if (status?.status !== "connected") { setDirectory(null); setModels([]); setSelected([]); }
    if (status?.status === "signed_out") setBrowserUrl("");
  }, [status?.status]);

  async function act(work: () => Promise<void>) {
    const current = generation.current;
    setBusy(true); setError(false); setNotice("");
    try { await work(); }
    catch { if (current === generation.current) { setError(true); setDirectory(null); } }
    finally { if (current === generation.current) setBusy(false); }
  }
  const disabled = busy || !plugin.enabled;
  const connected = status?.status === "connected";
  return <section className="ae-section-card" aria-label={tx("节点提供方", "Peer provider")}>
    <h3>{tx("节点提供方", "Peer provider")}</h3>
    <p>{tx("登录后连接此插件提供的节点和模型。发布本机模型需要单独选择。", "Connect to nodes and models from this plugin after signing in. Publishing local models requires a separate selection.")}</p>
    <p role="status">{!plugin.enabled ? tx("请先启用插件。", "Enable the plugin first.") : !status ? tx("正在读取状态…", "Loading status…") : connected ? status.bound ? tx("已登录，节点已绑定", "Signed in · node bound") : tx("已登录，节点未绑定", "Signed in · node not bound") : status.status === "pending" ? tx("等待浏览器完成登录", "Waiting for browser sign-in") : tx("未登录", "Signed out")}</p>
    {error ? <p role="alert">{tx("操作未完成，请重试或重新登录。", "The operation could not be completed. Retry or sign in again.")}</p> : null}
    {notice ? <p role="status">{notice}</p> : null}
    <div className="ae-inline-actions">
      {!connected ? <button className="ae-btn" disabled={disabled || status?.status === "pending"} onClick={() => void act(async () => {
        const current = generation.current;
        const login = await peerProviders.login(plugin.id);
        if (current !== generation.current) return;
        setBrowserUrl(login.browserUrl); setStatus({ plugin_id: plugin.id, status: "pending", bound: false, selected_service_ids: [] });
      })}>{tx("登录", "Sign in")}</button> : null}
      <button className="ae-btn" disabled={disabled} onClick={() => void act(async () => {
        const current = generation.current, next = await peerProviders.status(plugin.id);
        if (current === generation.current) { setStatus(next); setSelected(next.selected_service_ids); }
      })}>{tx("查看状态", "Check status")}</button>
      {connected ? <>
        <button className="ae-btn" disabled={disabled} onClick={() => void act(async () => {
          const current = generation.current; await peerProviders.bind(plugin.id); const next = await peerProviders.status(plugin.id);
          if (current === generation.current) setStatus(next);
        })}>{tx("绑定本机节点", "Bind this node")}</button>
        <button className="ae-btn" disabled={disabled || !status.bound} onClick={() => void act(async () => {
          const current = generation.current, next = await peerProviders.refresh(plugin.id);
          if (current === generation.current) setDirectory(next);
        })}>{tx("刷新目录", "Refresh directory")}</button>
      </> : null}
      {status && status.status !== "signed_out" ? <button className="ae-btn" disabled={disabled} onClick={() => void act(async () => {
        const current = generation.current; await peerProviders.logout(plugin.id);
        if (current === generation.current) { setStatus({ plugin_id: plugin.id, status: "signed_out", bound: false, selected_service_ids: [] }); setDirectory(null); setModels([]); setSelected([]); setBrowserUrl(""); }
      })}>{tx("退出登录", "Sign out")}</button> : null}
    </div>
    {browserUrl ? <p className="ae-peer-browser-link"><a className="ae-btn ae-btn-sm" href={browserUrl} target="_blank" rel="noopener noreferrer">{tx("在浏览器中继续登录", "Continue sign-in in browser")}</a></p> : null}
    {directory ? <div className="ae-peer-directory">
      <p className="ae-peer-directory-summary">{tx(`${directory.memberCount} 位成员 · ${directory.deviceCount} 个节点`, `${directory.memberCount} members · ${directory.deviceCount} nodes`)}</p>
      {directory.members && directory.members.length > 0 ? <div className="ae-peer-member-grid">
        {directory.members.map(member => <div key={member.id} className="ae-peer-member-chip">
          <strong>{member.name}</strong>
          <span>{member.devices.length ? member.devices.map(d => d.name).join(", ") : tx("无在线设备", "No bound node")}</span>
        </div>)}
      </div> : null}
      {directory.modelNames.length > 0 ? <ul className="ae-peer-model-list">{directory.modelNames.map((name, index) => <li key={index}>{name}</li>)}</ul> : null}
    </div> : null}
    {connected && status.bound ? <div className="ae-peer-publish-area">
      <div className="ae-inline-actions">
        <button className="ae-btn" disabled={disabled} onClick={() => void act(async () => {
          const current = generation.current, next = await peerProviders.models(plugin.id);
          if (current === generation.current) { setModels(next); setSelected(status.selected_service_ids); if (!next.length) setNotice(tx("没有可发布的本机模型。", "No local models are available to publish.")); }
        })}>{tx("选择要发布的本机模型", "Choose local models to publish")}</button>
        {status.selected_service_ids.length > 0 ? <button className="ae-btn" disabled={disabled} onClick={() => void act(async () => {
          const current = generation.current; await peerProviders.publish(plugin.id, []); const next = await peerProviders.status(plugin.id);
          if (current === generation.current) { setStatus(next); setSelected([]); setNotice(tx("已停止发布模型。", "Model publication stopped.")); }
        })}>{tx("停止发布全部模型", "Stop publishing all models")}</button> : null}
      </div>
      {models.length > 0 ? <fieldset className="ae-subcard-fieldset" disabled={disabled}><legend>{tx("只发布选中的模型", "Publish only selected models")}</legend>
        <div className="ae-checkbox-list">
          {models.map(model => <label key={model.service_id} className="ae-checkbox-item"><input type="checkbox" checked={selected.includes(model.service_id)} onChange={event => setSelected(previous => event.target.checked ? [...previous, model.service_id] : previous.filter(id => id !== model.service_id))} /> <span>{model.name}</span></label>)}
        </div>
        <div className="ae-inline-actions">
          <button className="ae-btn ae-btn-primary" onClick={() => void act(async () => {
            const current = generation.current; await peerProviders.publish(plugin.id, selected); const next = await peerProviders.status(plugin.id);
            if (current === generation.current) { setStatus(next); setSelected(next.selected_service_ids); setNotice(tx("已保存发布选择。", "Publication selection saved.")); }
          })}>{tx("保存发布选择", "Save publication selection")}</button>
        </div>
      </fieldset> : null}
    </div> : null}
  </section>;
}
