import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Check, Cloud, Ellipsis, LockKeyhole, Monitor, RefreshCw, Search, Share2, Sparkles, Terminal } from "lucide-react";
import { useAppContext } from "../appContext";
import { sourceRequest, type AISource } from "../domain/aiSources";
import { canUseAgentModel, rynAgent, type AgentModel, type AgentSettings } from "../domain/rynAgent";
import type { CLIServicesStatus, CLIServiceStatus, LLMServiceRecord } from "../domain/nodeClient";
import type { NativeModelStatus } from "../domain/nativeModel";
import { Modal } from "../personal/components";
import { personalHref } from "../personal/model";
import { useUILanguage } from "../uiI18n";
import { nodeControlUrl } from "../domain/nodeUrl";
import "./ModelPool.css";
import ModelPoolSettings from "./ModelPoolSettings";
import ModelPoolSharing from "./ModelPoolSharing";

type CatalogModel = AgentModel & { source_id?: string; model_id?: string; service_id?: string; peer_id?: string; adapter?: string; source_name?: string; sharing?: boolean; share_scope?: string };
type PoolRow = { key: string; name: string; origin: string; owner: "source" | "cli" | "native" | "peer"; source?: AISource; cli?: CLIServiceStatus; peer?: LLMServiceRecord; catalog?: CatalogModel; sharing: boolean };
type Props = {
  sources: AISource[]; cli: CLIServicesStatus | null; native: NativeModelStatus | null; peers: LLMServiceRecord[];
  loading: boolean; error: string; onRefresh: () => Promise<unknown>;
  onEditSource: (source: AISource) => void; onCLI: (kind: CLIServiceStatus["kind"]) => void; onNative: () => void;
};

export default function ModelPool({ sources, cli, native, peers, loading, error, onRefresh, onEditSource, onCLI, onNative }: Props) {
  const lang = useUILanguage();
  const text = (zh: string, en: string) => lang.startsWith("zh") ? zh : en;
  const { client } = useAppContext();
  const href = (path: string) => personalHref(path, client.mode === "fixture");
  const [catalog, setCatalog] = useState<CatalogModel[]>([]);
  const [settings, setSettings] = useState<AgentSettings | null>(null);
  const [agentError, setAgentError] = useState(false);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState("");
  const [feedback, setFeedback] = useState("");
  const [shareRow, setShareRow] = useState<PoolRow | null>(null);
  const [parameterRow, setParameterRow] = useState<PoolRow | null>(null);
  const [shareEnabled, setShareEnabled] = useState(false);
  const mounted = useRef(true);
  const refreshAgent = useCallback(async () => {
    const result = await Promise.allSettled([rynAgent.models(), rynAgent.settings()]);
    if (!mounted.current) return;
    if (result[0].status === "fulfilled") setCatalog(result[0].value.models as CatalogModel[]);
    if (result[1].status === "fulfilled") setSettings(result[1].value);
    setAgentError(result.some(item => item.status === "rejected"));
  }, []);
  // Configuration edits invalidate model capabilities; refetch rather than infer them from a name.
  useEffect(() => { mounted.current = true; void refreshAgent(); return () => { mounted.current = false; }; }, [refreshAgent, sources, native?.id, native?.model_settings?.rule_revision, native?.state]);
  const rows = useMemo(() => {
    const items: PoolRow[] = sources.flatMap(source => source.models.map(model => ({
      key: `${source.id}:${model}`, name: model, origin: source.name, owner: "source" as const, source,
      catalog: catalog.find(row => row.source_id === source.id && row.model_id === model),
      sharing: source.shared_models ? source.shared_models.includes(model) : source.sharing,
    })));
    for (const entry of cli?.services || []) if (entry.configured) items.push({
      key: entry.kind, name: entry.title, origin: entry.kind === "codex_cli" ? "Codex CLI" : entry.kind === "antigravity_cli" ? "Antigravity" : "Claude Code CLI", owner: "cli", cli: entry,
      catalog: catalog.find(row => row.id === `local/${entry.service_id}` || row.service_id === entry.service_id && row.source !== "peer"),
      sharing: entry.publication_enabled,
    });
    if (native && (native.installed || native.state !== "not_installed")) items.push({
      key: `native:${native.id}`, name: native.name, origin: "Ryn", owner: "native", sharing: native.sharing,
      catalog: catalog.find(row => row.id === `local/${native.id}` || row.service_id === native.id && row.source !== "peer"),
    });
    for (const peer of peers) items.push({
      key: `${peer.peer_id}/${peer.service.package_id}`, name: peer.service.model_alias, origin: peer.node_name || peer.peer_id,
      owner: "peer", peer, sharing: true,
      catalog: catalog.find(row => row.peer_id === peer.peer_id && row.service_id === peer.service.package_id),
    });
    // Include discoverable model providers even if they have no source-editor representation.
    for (const row of catalog) if (!items.some(item => item.catalog?.id === row.id) && !row.source_id) items.push({
      key: row.id, name: row.name, origin: row.source_name || row.source, owner: row.source === "peer" ? "peer" : "native",
      catalog: row, sharing: Boolean(row.sharing),
    });
    return items;
  }, [sources, cli, native, peers, catalog]);
  const visible = rows.filter(row => `${row.name} ${row.origin}`.toLowerCase().includes(query.toLowerCase()));
  const originLabel = (row: PoolRow) => row.owner === "peer" ? text("其他设备提供", "From another device") : row.owner === "source" && row.source?.kind === "api" ? text("我接入的 API", "My API source") : text("本机运行", "Runs locally");
  const run = async (key: string, task: () => Promise<void>) => {
    if (busy) return; setBusy(key); setFeedback("");
    try { await task(); } catch (reason) { setFeedback(reason instanceof Error ? reason.message : text("操作失败，请重试。", "Operation failed. Try again.")); }
    finally { if (mounted.current) setBusy(""); }
  };
  const choose = (row: PoolRow) => void run(row.key, async () => {
    if (!row.catalog || !canUseAgentModel(row.catalog)) return;
    setSettings(await rynAgent.updateSettings({ model_id: row.catalog.id }));
    setFeedback(text("助手默认模型已更新，分享设置保持不变。", "Default model updated. Sharing is unchanged."));
  });
  const probe = (row: PoolRow) => void run(row.key, async () => {
    let result: { tool_support: string };
    if (row.source) {
      result = await sourceRequest(`/${encodeURIComponent(row.source.id)}/models/test`, { model_id: row.name });
    } else if (row.catalog?.source === "local_service" && row.catalog.service_id) {
      const response = await fetch(nodeControlUrl(`/llm/services/${encodeURIComponent(row.catalog.service_id)}/tools/test`), { method: "POST", credentials: "include" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || text("工具调用验证失败", "Tool verification failed"));
      result = body;
    } else return;
    await onRefresh(); await refreshAgent();
    setFeedback(result.tool_support === "supported" ? text("工具调用验证通过，可选择或重新确认用于助手。", "Tools verified. Select or reconfirm this model for the assistant.") : text("此模型未通过工具调用验证，仍可用于普通推理。", "Tool verification failed; ordinary inference is still available."));
  });
  const canProbe = (row: PoolRow) => row.source
    ? row.source.enabled && (row.source.kind === "local" || row.source.has_key)
    : row.catalog?.source === "local_service" && row.catalog.available && row.catalog.adapter !== "claude_cli";
  const editRow = (row: PoolRow) => {
    if (row.source) onEditSource(row.source); else if (row.cli) onCLI(row.cli.kind); else if (row.owner === "native") onNative();
  };
  const saveShare = () => void run("share", async () => {
    if (!shareRow?.source) return;
    // Re-read so an unrelated model's latest share choice is preserved.
    const latest = await sourceRequest<{ sources: AISource[] }>();
    const source = latest.sources.find(item => item.id === shareRow.source?.id);
    if (!source || !source.models.includes(shareRow.name)) throw new Error(text("模型配置已变化，请刷新后重试。", "Model configuration changed. Refresh and try again."));
    const selected = new Set(source.shared_models ?? (source.sharing ? source.models : []));
    if (shareEnabled) selected.add(shareRow.name); else selected.delete(shareRow.name);
    await sourceRequest(`/${encodeURIComponent(source.id)}`, { shared_models: [...selected] }, "PUT");
    setShareRow(null); await onRefresh(); await refreshAgent();
    setFeedback(text("这个模型的分享设置已保存，助手选择保持不变。", "Sharing saved for this model. The assistant selection is unchanged."));
  });
  return <>
    <section className="mp-agent-strip" aria-label={text("可选助手", "Optional assistant")}>
      <span className="mp-agent-icon"><Sparkles size={19} /></span><div><strong>{text("Ryn 助手", "Ryn Agent")}</strong><small>{settings?.enabled ? text("已启用", "Enabled") : text("可选功能", "Optional")}</small><p>{settings?.enabled ? text("默认使用 ", "Default model: ") + (catalog.find(model => model.id === settings.model_id)?.name || text("尚未选择", "Not selected")) : text("让 Ryn 帮你处理任务，也可以继续手动操作。", "Let Ryn handle tasks, or keep using the manual controls.")}</p></div>
      <Link to={href("/agent")}>{settings?.enabled ? text("打开助手 →", "Open assistant →") : text("了解并启用 →", "Explore and enable →")}</Link>
    </section>
    <div className="mp-toolbar"><label className="ais-search"><Search size={17} /><input aria-label={text("搜索模型", "Search models")} placeholder={text("搜索模型或来源", "Search models or sources")} value={query} onChange={event => setQuery(event.target.value)} /><button aria-label={text("刷新模型", "Refresh models")} disabled={Boolean(busy)} onClick={() => void run("refresh", async () => { await onRefresh(); await refreshAgent(); })}><RefreshCw size={16} /></button></label><small>{rows.length} {text("个模型", "models")}</small></div>
    {(error || feedback) && <p role={error ? "alert" : "status"} className="mp-feedback">{error || feedback}</p>}
    {agentError && <p className="mp-note" role="status">{text("暂时无法读取助手状态，模型配置和分享仍可使用。", "Assistant status is unavailable. Model configuration and sharing remain available.")}</p>}
    {loading ? <p role="status">{text("正在读取模型…", "Loading models…")}</p> : <div className="mp-list" aria-label={text("模型池", "Model pool")}>
      {visible.map(row => <div className="mp-model" key={row.key}>
        <span className={`ais-brand ${row.source?.provider === "zai" ? "ais-mark ais-zai" : row.cli?.kind === "codex_cli" ? "ais-openai" : ""}`}>{row.source?.provider === "zai" || row.cli?.kind === "codex_cli" ? null : row.owner === "peer" ? <Monitor size={23} /> : row.cli ? <Terminal size={23} /> : <Cloud size={23} />}</span>
        <div className="mp-model-name"><strong>{row.name}</strong><small>{row.origin} · {originLabel(row)}</small>{row.catalog?.description && (!row.catalog.available || !row.catalog.agent_supported) && <span className="mp-capability">{row.catalog.description}</span>}</div>
        <div className="mp-model-actions">
          {row.source ? <button className="mp-share" aria-label={`${text("分享", "Share")} ${row.name}`} disabled={Boolean(busy)} onClick={() => { setShareRow(row); setShareEnabled(row.sharing); setFeedback(""); }}>{row.sharing ? <Share2 size={14} /> : <LockKeyhole size={14} />}{row.sharing ? text("已分享", "Shared") : text("仅自己使用", "Private")}</button> : row.owner === "peer" ? <small>{text("由对方分享", "Shared by provider")}</small> : <button className="mp-share" disabled={Boolean(busy)} onClick={() => editRow(row)}>{row.sharing ? <Share2 size={14} /> : <LockKeyhole size={14} />}{row.sharing ? text("已分享", "Shared") : text("仅自己使用", "Private")}</button>}
          {settings?.model_id && settings.model_id === row.catalog?.id && <span className="mp-default"><Check size={14} />{text("助手默认", "Assistant default")}</span>}
          {row.source?.model_settings?.[row.name] && <button className="ais-secondary" disabled={Boolean(busy)} onClick={() => setParameterRow(row)}>{text("此模型参数", "Model parameters")}</button>}
          {row.catalog && canUseAgentModel(row.catalog) ? <button className="ais-secondary" disabled={Boolean(busy)} onClick={() => choose(row)}>{settings?.model_id === row.catalog.id ? text("重新确认用于助手", "Reconfirm for assistant") : text("用于助手", "Use for assistant")}</button> : canProbe(row) ? <button className="ais-secondary" disabled={Boolean(busy)} onClick={() => probe(row)}>{busy === row.key ? text("验证中…", "Verifying…") : text("验证工具调用", "Verify tool calls")}</button> : <span className="mp-unavailable">{text("助手暂不可用", "Agent unavailable")}</span>}
        </div>
        {row.owner !== "peer" && <button className="ais-icon" aria-label={`${text("配置", "Configure")} ${row.name}`} onClick={() => editRow(row)}><Ellipsis size={18} /></button>}
      </div>)}
      {!visible.length && <p className="ais-empty">{query ? text("没有找到匹配的模型。", "No matching models.") : text("添加来源并选择模型，即可在这里管理。", "Connect a source and choose models to manage them here.")}</p>}
    </div>}
    <p className="mp-note">{text("助手默认模型与对外分享分别设置。停用助手，不影响手动配置和模型调用。", "Assistant defaults and sharing are independent. Disabling the assistant leaves manual configuration and inference available.")}</p>
    <p className="mp-note">{text("修改模型参数后若出现“验证工具调用”，请先验证，再确认用于助手。", "If Verify tool calls appears after changing model parameters, verify first, then confirm the model for the assistant.")}</p>
    {parameterRow?.source && <ModelPoolSettings source={parameterRow.source} modelID={parameterRow.name} onClose={() => setParameterRow(null)} onSaved={async () => { await onRefresh(); await refreshAgent(); }} />}
    {shareRow?.source?.model_settings?.[shareRow.name]?.effective.share_scope ? <ModelPoolSharing source={shareRow.source} modelID={shareRow.name} serviceID={shareRow.catalog?.service_id} onClose={() => setShareRow(null)} onSaved={async () => { await onRefresh(); await refreshAgent(); }} /> : shareRow && <Modal title={`${text("分享", "Share")} ${shareRow.name}`} onClose={() => { if (!busy) setShareRow(null); }}><div className="ais-modal mp-share-dialog">
      <label className="ais-check"><input type="checkbox" checked={shareEnabled} disabled={Boolean(busy)} onChange={event => setShareEnabled(event.target.checked)} />{text("允许我的个人空间节点调用这个模型", "Allow nodes in my personal space to call this model")}</label>
      <p>{text("范围：已加入你的个人空间、且具备 AI 访问权限的节点。只改变这个模型，同一来源的其他模型不受影响。", "Scope: nodes in your personal space with AI access. This changes only this model, not other models from the same source.")}</p>
      <p className="ais-muted">{text("无状态转发推理请求，不附加本机助手的会话、记忆和工具。", "Inference requests are forwarded without adding this node’s assistant sessions, memory or tools.")}</p>
      <Link to={href("/services/agent-sharing")}>{text("管理节点与好友访问权限 →", "Manage node and friend access →")}</Link>
      {feedback && <p role="alert">{feedback}</p>}
      <div className="ais-modal-footer"><small>{text("不影响助手默认模型", "Does not change the assistant default")}</small><button className="ais-primary" disabled={Boolean(busy)} onClick={saveShare}>{busy ? text("保存中…", "Saving…") : text("保存", "Save")}</button></div>
    </div></Modal>}
  </>;
}
