import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Check, Cloud, Ellipsis, LockKeyhole, Monitor, RefreshCw, Search, Share2, Sparkles, Terminal } from "lucide-react";
import { useAppContext } from "../appContext";
import { formatTokenCount, sourceRequest, type AISource, type ModelTokenUsage } from "../domain/aiSources";
import { loadCompanySharing, loadCompanyUsage, setCompanyShared, type CompanySharingState, type CompanyUsage } from "../domain/companySharing";
import { canUseAgentModel, rynAgent, type AgentModel, type AgentSettings } from "../domain/rynAgent";
import type { CLIServicesStatus, CLIServiceStatus, LLMServiceRecord } from "../domain/nodeClient";
import type { NativeModelStatus } from "../domain/nativeModel";
import { Modal } from "../personal/components";
import { personalHref } from "../personal/model";
import { useUILanguage } from "../uiI18n";
import "./ModelPool.css";
import ModelPoolSettings, { buildDailySeries } from "./ModelPoolSettings";
import ModelPoolSharing from "./ModelPoolSharing";

type CatalogModel = AgentModel & { source_id?: string; model_id?: string; service_id?: string; peer_id?: string; adapter?: string; source_name?: string; sharing?: boolean; share_scope?: string };
/** A model a colleague shares through the company plugin: who provides it and whether their device answers. */
type CompanyOrigin = { provider: string; online: boolean };
type PoolRow = {
  key: string;
  name: string;
  origin: string;
  owner: "source" | "cli" | "native" | "peer";
  source?: AISource;
  cli?: CLIServiceStatus;
  peer?: LLMServiceRecord;
  catalog?: CatalogModel;
  sharing: boolean;
  usage?: ModelTokenUsage;
  usageKey?: string;
  /** Local service id that the company sharing switch publishes. */
  serviceId?: string;
  company?: CompanyOrigin;
};
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
  const [agentLoading, setAgentLoading] = useState({ models: true, settings: true });
  const [agentErrors, setAgentErrors] = useState({ models: false, settings: false });
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState("");
  const [feedback, setFeedback] = useState("");
  const [shareRow, setShareRow] = useState<PoolRow | null>(null);
  const [parameterRow, setParameterRow] = useState<PoolRow | null>(null);
  const [usageModelFilter, setUsageModelFilter] = useState<string>("all");
  const [usageDays, setUsageDays] = useState<7 | 30>(7);
  const [shareEnabled, setShareEnabled] = useState(false);
  const [company, setCompany] = useState<CompanySharingState | null>(null);
  const [companyUsage, setCompanyUsage] = useState<CompanyUsage | null>(null);
  const mounted = useRef(true);
  const refreshRevision = useRef(0);
  const settingsWriteRevision = useRef(0);
  const refreshAgent = useCallback(async () => {
    if (!mounted.current) return;
    const revision = ++refreshRevision.current;
    const settingsRevision = settingsWriteRevision.current;
    const current = () => mounted.current && revision === refreshRevision.current;
    const currentSettings = () => current() && settingsRevision === settingsWriteRevision.current;
    setAgentLoading({ models: true, settings: true });
    setAgentErrors({ models: false, settings: false });
    await Promise.allSettled([
      rynAgent.models().then(value => { if (current()) setCatalog(value.models as CatalogModel[]); })
        .catch(() => { if (current()) setAgentErrors(value => ({ ...value, models: true })); })
        .finally(() => { if (current()) setAgentLoading(value => ({ ...value, models: false })); }),
      rynAgent.settings().then(value => { if (currentSettings()) setSettings(value); })
        .catch(() => { if (currentSettings()) setAgentErrors(value => ({ ...value, settings: true })); })
        .finally(() => { if (currentSettings()) setAgentLoading(value => ({ ...value, settings: false })); }),
      // Company sharing is optional: without a signed-in publisher there is simply no control.
      loadCompanySharing().then(value => { if (current()) setCompany(value); }).catch(() => { if (current()) setCompany(null); }),
      loadCompanyUsage().then(value => { if (current()) setCompanyUsage(value); }).catch(() => { if (current()) setCompanyUsage(null); }),
    ]);
  }, []);
  // Reload connection readiness and protocol support after configuration edits.
  useEffect(() => { mounted.current = true; void refreshAgent(); return () => { mounted.current = false; }; }, [refreshAgent, sources, native?.id, native?.model_settings?.rule_revision, native?.state]);
  const rows = useMemo(() => {
    const items: PoolRow[] = sources.flatMap(source => source.models.map(model => {
      const cat = catalog.find(row => row.source_id === source.id && row.model_id === model);
      const usage = source.model_settings?.[model]?.usage || source.model_usage?.[model] || cat?.usage;
      return {
        key: `${source.id}:${model}`, name: model, origin: source.name, owner: "source" as const, source,
        catalog: cat, serviceId: cat?.service_id,
        sharing: source.shared_models ? source.shared_models.includes(model) : source.sharing,
        usage,
        usageKey: usage?.usage_key || `source/${source.id}/${model}`,
      };
    }));
    for (const entry of cli?.services || []) if (entry.configured) {
      const cat = catalog.find(row => row.id === `local/${entry.service_id}` || row.service_id === entry.service_id && row.source !== "peer");
      const usage = entry.usage || cat?.usage;
      items.push({
        key: entry.kind, name: `${entry.title} · CLI 默认`, origin: entry.kind === "codex_cli" ? "Codex CLI" : entry.kind === "antigravity_cli" ? "Antigravity" : "Claude Code CLI", owner: "cli", cli: entry,
        catalog: cat, serviceId: entry.service_id,
        sharing: entry.publication_enabled,
        usage,
        usageKey: usage?.usage_key || `local/${entry.service_id}`,
      });
    }
    for (const model of catalog.filter(row => row.cli_model && row.source === "local_service")) {
      const entry = cli?.services.find(item => item.service_id === model.service_id);
      const usage = model.usage || entry?.usage;
      items.push({
        key: model.id, name: model.name, origin: model.source_name || "CLI", owner: "cli", cli: entry,
        catalog: model, sharing: Boolean(entry?.publication_enabled),
        usage,
        usageKey: usage?.usage_key || model.id,
      });
    }
    if (native && (native.installed || native.state !== "not_installed")) {
      const cat = catalog.find(row => row.id === `local/${native.id}` || row.service_id === native.id && row.source !== "peer");
      const usage = native.usage || native.model_settings?.usage || cat?.usage;
      items.push({
        key: `native:${native.id}`, name: native.name, origin: "Ryn", owner: "native", sharing: native.sharing,
        catalog: cat, serviceId: native.id,
        usage,
        usageKey: usage?.usage_key || `local/${native.id}`,
      });
    }
    for (const peer of peers) {
      const cat = catalog.find(row => row.peer_id === peer.peer_id && row.service_id === peer.service.package_id);
      const usage = peer.usage || cat?.usage;
      items.push({
        key: `${peer.peer_id}/${peer.service.package_id}`, name: peer.service.model_alias, origin: peer.node_name || peer.peer_id,
        owner: "peer", peer, sharing: true,
        catalog: cat,
        ...(peer.access === "provider" || cat?.provider_resource ? { company: { provider: peer.provider_name || cat?.source_name || peer.node_name || "", online: cat ? cat.available : peer.online } } : {}),
        usage,
        usageKey: usage?.usage_key || cat?.id,
      });
    }
    // Include discoverable model providers even if they have no source-editor representation.
    for (const row of catalog) if (!items.some(item => item.catalog?.id === row.id) && !row.source_id) {
      items.push({
        key: row.id, name: row.name, origin: row.source_name || row.source, owner: row.source === "peer" ? "peer" : "native",
        catalog: row, sharing: Boolean(row.sharing),
        ...(row.provider_resource ? { company: { provider: row.source_name || "", online: row.available } } : {}),
        usage: row.usage,
        usageKey: row.usage?.usage_key || row.id,
      });
    }
    return items;
  }, [sources, cli, native, peers, catalog]);
  const visible = rows.filter(row => `${row.name} ${row.origin}`.toLowerCase().includes(query.toLowerCase()));
  const hasAnyUsage = useMemo(
    () => rows.some(row => Boolean(row.usage)),
    [rows],
  );
  const activeUsageRow = useMemo(
    () => (usageModelFilter === "all" ? null : rows.find(row => row.key === usageModelFilter) || null),
    [rows, usageModelFilter],
  );
  const filteredUsages = useMemo(
    () => (activeUsageRow ? [activeUsageRow.usage] : rows.map(row => row.usage)),
    [activeUsageRow, rows],
  );
  const dailySeries = useMemo(() => buildDailySeries(filteredUsages, usageDays), [filteredUsages, usageDays]);
  const cardTodayTokens = useMemo(
    () => filteredUsages.reduce((sum, u) => sum + (u?.today?.total_tokens || 0), 0),
    [filteredUsages],
  );
  const cardPeriodTokens = useMemo(
    () => dailySeries.reduce((sum, item) => sum + item.tokens, 0),
    [dailySeries],
  );
  const cardTotalTokens = useMemo(
    () => filteredUsages.reduce((sum, u) => sum + (u?.total?.total_tokens || 0), 0),
    [filteredUsages],
  );
  const maxDailyBar = useMemo(
    () => Math.max(1, ...dailySeries.map(item => item.tokens)),
    [dailySeries],
  );
  const companyRows = visible.filter(row => row.company);
  const ownRows = visible.filter(row => !row.company);
  const canShareWithCompany = (row: PoolRow) => Boolean(company && row.serviceId && !row.catalog?.cli_model && (company.eligible.includes(row.serviceId) || company.selected.includes(row.serviceId)));
  const companyUsageRows = companyUsage && (companyUsage.sharing || companyUsage.rows.length > 0) ? companyUsage.rows : null;
  const originLabel = (row: PoolRow) => row.company ? `${text("提供人", "Shared by")} ${row.company.provider || text("公司同事", "a colleague")} · ${row.company.online ? text("在线", "Online") : text("离线", "Offline")}` : row.owner === "peer" ? text("其他设备提供", "From another device") : row.owner === "source" && row.source?.kind === "api" ? text("我接入的 API", "My API source") : text("本机运行", "Runs locally");
  const renderUsageCounter = (row: PoolRow) => {
    const usage = row.usage;
    if (!usage) return null;
    const todayTokens = usage.today?.total_tokens || 0;
    const totalTokens = usage.total?.total_tokens || 0;
    const isSelected = usageModelFilter === row.key;
    return (
      <button
        type="button"
        className={`mp-usage-counter${isSelected ? " is-active" : ""}`}
        data-testid={`model-usage-${row.key}`}
        onClick={() => setUsageModelFilter(current => (current === row.key ? "all" : row.key))}
      >
        <span>{text("今日", "Today")} <strong>{formatTokenCount(todayTokens)}</strong></span>
        <i aria-hidden="true">·</i>
        <span>{text("累计", "Total")} <strong>{formatTokenCount(totalTokens)}</strong></span>
      </button>
    );
  };
  const run = async (key: string, task: () => Promise<void>) => {
    if (busy) return; setBusy(key); setFeedback("");
    try { await task(); } catch (reason) { setFeedback(reason instanceof Error ? reason.message : text("操作失败，请重试。", "Operation failed. Try again.")); }
    finally { if (mounted.current) setBusy(""); }
  };
  const choose = (row: PoolRow) => void run(row.key, async () => {
    if (!row.catalog || !canUseAgentModel(row.catalog)) return;
    const updated = await rynAgent.updateSettings({ model_id: row.catalog.id });
    ++settingsWriteRevision.current;
    setSettings(updated);
    setAgentLoading(value => ({ ...value, settings: false }));
    setAgentErrors(value => ({ ...value, settings: false }));
    setFeedback(text("助手默认模型已更新，分享设置保持不变。", "Default model updated. Sharing is unchanged."));
  });
  const unavailableLabel = (row: PoolRow) => !row.catalog ? agentLoading.models ? text("正在读取助手模型…", "Loading assistant models…") : text("助手状态暂不可用", "Assistant status unavailable")
    : !row.catalog.available ? text("模型当前不可用", "Model unavailable")
    : row.catalog.source === "peer" && row.catalog.destination_binding_supported === false ? text("接入信息暂不可核对", "Destination unavailable")
    : text("不支持助手工具", "Assistant tools unsupported");
  const editRow = (row: PoolRow) => {
    if (row.source) onEditSource(row.source); else if (row.cli) onCLI(row.cli.kind); else if (row.owner === "native") onNative();
  };
  const toggleCompany = (row: PoolRow, shared: boolean) => void run(`company:${row.key}`, async () => {
    if (!company || !row.serviceId) return;
    let selected: string[];
    try { selected = await setCompanyShared(company.pluginId, row.serviceId, shared); }
    catch {
      // Show what is actually saved, never the attempted value.
      setCompany(await loadCompanySharing().catch(() => null));
      throw new Error(text("公司同事共享没有保存，请稍后重试。", "Company sharing was not saved. Try again later."));
    }
    setCompany(value => value && { ...value, selected });
    setCompanyUsage(await loadCompanyUsage().catch(() => null));
    setFeedback(shared ? text(`已把 ${row.name} 共享给公司同事。家庭组和好友设置不变。`, `${row.name} is now shared with company colleagues. Personal space and friend settings are unchanged.`)
      : text(`已停止向公司同事共享 ${row.name}。`, `${row.name} is no longer shared with company colleagues.`));
  });
  const renderRow = (row: PoolRow) => <div className="mp-model" key={row.key}>
        <span className={`ais-brand ${row.source?.provider === "zai" ? "ais-mark ais-zai" : row.cli?.kind === "codex_cli" ? "ais-openai" : ""}`}>{row.source?.provider === "zai" || row.cli?.kind === "codex_cli" ? null : row.owner === "peer" ? <Monitor size={23} /> : row.cli ? <Terminal size={23} /> : <Cloud size={23} />}</span>
        <div className="mp-model-name">
          <strong>{row.name}</strong>
          <div className="mp-model-meta">
            <small>{row.company ? originLabel(row) : `${row.origin} · ${originLabel(row)}`}</small>
            {renderUsageCounter(row)}
          </div>
          {row.catalog?.description && (!row.catalog.available || !row.catalog.agent_supported) && <span className="mp-capability">{row.catalog.description}</span>}
        </div>
        <div className="mp-model-actions">
          {canShareWithCompany(row) && <label className="mp-company-share" title={text("公司同事只得到模型的文字回复和工具调用建议，命令在对方自己的电脑上执行。", "Colleagues receive text and tool-call suggestions only; commands run on their own computer.")}><input type="checkbox" aria-label={`${text("共享给公司同事", "Share with company colleagues")} ${row.name}`} checked={company!.selected.includes(row.serviceId!)} disabled={Boolean(busy)} onChange={event => toggleCompany(row, event.target.checked)} />{text("公司同事", "Company")}</label>}
          {row.source ? <button className="mp-share" aria-label={`${text("分享", "Share")} ${row.name}`} disabled={Boolean(busy)} onClick={() => { setShareRow(row); setShareEnabled(row.sharing); setFeedback(""); }}>{row.sharing ? <Share2 size={14} /> : <LockKeyhole size={14} />}{row.sharing ? text("已分享", "Shared") : text("仅自己使用", "Private")}</button> : row.owner === "peer" ? <small>{row.company ? text("公司共享", "Company shared") : text("由对方分享", "Shared by provider")}</small> : <button className="mp-share" disabled={Boolean(busy)} onClick={() => editRow(row)}>{row.sharing ? <Share2 size={14} /> : <LockKeyhole size={14} />}{row.sharing ? text("已分享", "Shared") : text("仅自己使用", "Private")}</button>}
          {settings?.model_id && settings.model_id === row.catalog?.id && <span className="mp-default"><Check size={14} />{text("助手默认", "Assistant default")}</span>}
          {row.source?.model_settings?.[row.name] && (
            <button className="ais-secondary" disabled={Boolean(busy)} onClick={() => setParameterRow(row)}>{text("此模型参数", "Model parameters")}</button>
          )}
          {row.catalog && canUseAgentModel(row.catalog) ? <button className="ais-secondary" disabled={Boolean(busy)} onClick={() => choose(row)}>{settings?.model_id === row.catalog.id ? text("重新确认用于助手", "Reconfirm for assistant") : text("用于助手", "Use for assistant")}</button> : <span className="mp-unavailable">{unavailableLabel(row)}</span>}
        </div>
        {row.owner !== "peer" && <button className="ais-icon" aria-label={`${text("配置", "Configure")} ${row.name}`} onClick={() => editRow(row)}><Ellipsis size={18} /></button>}
      </div>;
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
      <span className="mp-agent-icon"><Sparkles size={19} /></span><div><strong>{text("Ryn 助手", "Ryn Agent")}</strong><small>{!settings ? agentLoading.settings ? text("正在读取助手状态…", "Loading assistant status…") : text("助手状态暂不可用", "Assistant status unavailable") : settings.enabled ? text("已启用", "Enabled") : text("可选功能", "Optional")}</small>{settings && <p>{settings.enabled ? text("默认使用 ", "Default model: ") + (catalog.find(model => model.id === settings.model_id)?.name || settings.model_id || text("尚未选择", "Not selected")) : text("让 Ryn 帮你处理任务，也可以继续手动操作。", "Let Ryn handle tasks, or keep using the manual controls.")}</p>}</div>
      <Link to={href("/agent")}>{!settings || settings.enabled ? text("打开助手 →", "Open assistant →") : text("了解并启用 →", "Explore and enable →")}</Link>
    </section>
    {hasAnyUsage && (
      <section className="mp-codex-usage-card" aria-label={text("Token 每日用量", "Daily token usage")}>
        <div className="mp-codex-top">
          <div className="mp-codex-stats">
            <div className="mp-codex-stat">
              <span>{text("今日用量", "Today")}</span>
              <strong data-testid="codex-usage-today">{formatTokenCount(cardTodayTokens)}</strong>
            </div>
            <div className="mp-codex-stat">
              <span>{usageDays === 7 ? text("近 7 天", "Last 7 days") : text("近 30 天", "Last 30 days")}</span>
              <strong data-testid="codex-usage-period">{formatTokenCount(cardPeriodTokens)}</strong>
            </div>
            <div className="mp-codex-stat">
              <span>{text("累计总计", "Total")}</span>
              <strong data-testid="codex-usage-total">{formatTokenCount(cardTotalTokens)}</strong>
            </div>
          </div>
          <div className="mp-codex-controls">
            <select
              className="mp-codex-select"
              aria-label={text("按模型查看用量", "Filter usage by model")}
              value={activeUsageRow ? activeUsageRow.key : "all"}
              onChange={event => setUsageModelFilter(event.target.value)}
            >
              <option value="all">{text("全部模型", "All models")}</option>
              {rows.map(row => (
                <option key={row.key} value={row.key}>
                  {row.name} ({row.origin})
                </option>
              ))}
            </select>
            <div className="mp-codex-period" role="group" aria-label={text("统计天数", "Usage period")}>
              <button
                type="button"
                className={usageDays === 7 ? "is-active" : ""}
                onClick={() => setUsageDays(7)}
              >
                {text("7 天", "7d")}
              </button>
              <button
                type="button"
                className={usageDays === 30 ? "is-active" : ""}
                onClick={() => setUsageDays(30)}
              >
                {text("30 天", "30d")}
              </button>
            </div>
          </div>
        </div>
        <div className={`mp-codex-bars${usageDays === 30 ? " is-30d" : ""}`} role="img" aria-label={text("每日 Token 用量柱状图", "Daily token usage bar chart")}>
          {dailySeries.map((item, idx) => {
            const heightPct = item.tokens > 0 ? Math.max(10, Math.round((item.tokens / maxDailyBar) * 100)) : 3;
            const showDayLabel = usageDays === 7 || item.isToday || idx % 5 === 0;
            return (
              <div key={item.day} className={`mp-codex-bar-col${item.isToday ? " is-today" : ""}`} title={`${item.day}: ${item.tokens.toLocaleString()} tokens`}>
                {usageDays === 7 && (
                  <small className="mp-codex-bar-val">{item.tokens > 0 ? formatTokenCount(item.tokens) : "0"}</small>
                )}
                <div className="mp-codex-bar-track">
                  <div className="mp-codex-bar-fill" style={{ height: `${heightPct}%` }} />
                </div>
                <span className="mp-codex-bar-day">
                  {showDayLabel ? (item.isToday ? text("今天", "Today") : item.label) : ""}
                </span>
              </div>
            );
          })}
        </div>
      </section>
    )}
    {companyUsageRows && (
      <section className="mp-company-usage" aria-label={text("公司同事用量", "Company colleague usage")}>
        <header><strong>{text("公司同事用量", "Company colleague usage")}</strong><small>{text("只统计本机共享出去的模型，不设限额。其他同事电脑上的共享由各自电脑统计。", "Only models shared from this device, with no limits. Other devices keep their own records.")}</small></header>
        {companyUsageRows.length ? <table>
          <thead><tr><th>{text("同事", "Colleague")}</th><th>{text("模型", "Model")}</th><th>{text("今天请求", "Requests today")}</th><th>{text("今天 Token", "Tokens today")}</th><th>{text("近 30 天请求", "Requests, 30 days")}</th><th>{text("近 30 天 Token", "Tokens, 30 days")}</th></tr></thead>
          <tbody>{companyUsageRows.map(row => <tr key={`${row.model_key}:${row.peer_id}`}>
            <td title={row.peer_id}>{row.member_name || `${text("未知设备", "Unknown device")} ${row.peer_id.slice(0, 8)}`}</td><td>{row.model_name || row.model_key}</td>
            <td>{row.today.requests}</td><td>{formatTokenCount(row.today.total_tokens)}</td>
            <td>{row.last_30_days.requests}</td><td>{formatTokenCount(row.last_30_days.total_tokens)}</td>
          </tr>)}</tbody>
        </table> : <p>{text("还没有同事使用。", "No colleague has used these models yet.")}</p>}
      </section>
    )}
    <div className="mp-toolbar"><label className="ais-search"><Search size={17} /><input aria-label={text("搜索模型", "Search models")} placeholder={text("搜索模型或来源", "Search models or sources")} value={query} onChange={event => setQuery(event.target.value)} /><button aria-label={text("刷新模型", "Refresh models")} disabled={Boolean(busy)} onClick={() => void run("refresh", async () => { await onRefresh(); await refreshAgent(); })}><RefreshCw size={16} /></button></label><small>{rows.length} {text("个模型", "models")}</small></div>
    {(error || feedback) && <p role={error ? "alert" : "status"} className="mp-feedback">{error || feedback}</p>}
    {(agentErrors.models || agentErrors.settings) && <p className="mp-note" role="status">{text("暂时无法读取助手状态，模型配置和分享仍可使用。", "Assistant status is unavailable. Model configuration and sharing remain available.")}</p>}
    {loading ? <p role="status">{text("正在读取模型…", "Loading models…")}</p> : <div className="mp-list" aria-label={text("模型池", "Model pool")}>
      {ownRows.map(renderRow)}
      {companyRows.length > 0 && <h3 className="mp-group-title">{text("公司共享", "Company shared")}<small>{text("由公司同事通过公司节点共享，命令只在你的电脑上执行", "Shared by colleagues through the company node; commands run only on your computer")}</small></h3>}
      {companyRows.map(renderRow)}
      {!visible.length && <p className="ais-empty">{query ? text("没有找到匹配的模型。", "No matching models.") : text("添加来源并选择模型，即可在这里管理。", "Connect a source and choose models to manage them here.")}</p>}
    </div>}
    <p className="mp-note">{text("助手默认模型与对外分享分别设置。停用助手，不影响手动配置和模型调用。", "Assistant defaults and sharing are independent. Disabling the assistant leaves manual configuration and inference available.")}</p>
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
