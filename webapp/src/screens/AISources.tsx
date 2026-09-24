import { tr, useUILanguage } from "../uiI18n";
import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Cloud, Terminal, Box, Plus, Search, ChevronRight, Laptop, Monitor, RefreshCw, Check, Settings2, Share2, Eye, EyeOff, ShieldCheck, Trash2, Power, Pencil, ArrowRight, Code2, Server } from "lucide-react";
import { useAppContext } from "../appContext";
import { Modal } from "../personal/components";
import { newSource, sourcePresets, sourceRequest, type AISource } from "../domain/aiSources";
import type { CLIServicesStatus, CLIServiceStatus, LLMServiceRecord } from "../domain/nodeClient";
import WorkspaceSelect from "./WorkspaceSelect";
import "./AISources.css";
import NativeModelPanel from "./components/NativeModelPanel";
import { nativeBusy, nativeError, nativeModelRequest, nativeState, type NativeModelStatus } from "../domain/nativeModel";
import { personalHref } from "../personal/model";

function Brand({ provider }: { provider: string }) {
  useUILanguage();
  if (["openai", "codex_cli"].includes(provider)) return <span className="ais-brand ais-openai" aria-label="ChatGPT" />;
  if (["zai", "claude_cli", "deepseek", "qwen", "ollama", "lmstudio", "openrouter"].includes(provider)) return <span className={`ais-brand ais-mark ais-${provider}`} aria-label={provider} />;
  return <span className="ais-brand"><Cloud size={24} /></span>;
}
const statusLabel = (s: AISource) => !s.enabled ? tr("已停用") : s.status === "needs_key" ? tr("待填写密钥") : s.status === "ready" ? tr("已验证") : tr("待测试");

export default function AISources() {
  useUILanguage();
  const { client, confirm, notify } = useAppContext();
  const [params, setParams] = useSearchParams();
  const [sources, setSources] = useState<AISource[]>([]);
  const [cli, setCLI] = useState<CLIServicesStatus | null>(null);
  const [peers, setPeers] = useState<LLMServiceRecord[]>([]);
  const [device, setDevice] = useState(tr("本机"));
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [modal, setModal] = useState<"choose" | "source" | "cli" | "native" | null>(null);
  const [native, setNative] = useState<NativeModelStatus | null>(null);
  const [nativeLoadError, setNativeLoadError] = useState("");
  useEffect(() => {
    let closed = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try { const value = await nativeModelRequest(); if (!closed) { setNative(value); setNativeLoadError(""); } }
      catch { if (!closed) setNativeLoadError(tr("无法读取本地模型状态，请检查节点连接。")); }
      if (!closed) timer = setTimeout(poll, 2000);
    };
    void poll();
    return () => { closed = true; clearTimeout(timer); };
  }, []);
  const [draft, setDraft] = useState<AISource>(newSource());
  const [kind, setKind] = useState<CLIServiceStatus["kind"]>("codex_cli");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [modelText, setModelText] = useState("");
  const [defaultsText, setDefaultsText] = useState("{}");
  const [foundModels, setFoundModels] = useState<string[]>([]);
  const refresh = useCallback(async () => {
    try {
      const [data, cliData, services] = await Promise.all([
        sourceRequest<{ sources: AISource[]; node_name: string; peer_id: string }>(), client.getCLIServices(), client.listLLMServices(),
      ]);
      setSources(data.sources); setDevice(data.node_name); setCLI(cliData);
      setPeers(services.filter(s => s.peer_id !== data.peer_id)); setError("");
      return data.sources;
    } catch (e) { setError((e as Error).message); return []; }
    finally { setLoading(false); }
  }, [client]);
  const edit = (value: AISource) => {
    setDraft({ ...value, api_key: "" }); setModelText(value.models.join("\n")); setDefaultsText(JSON.stringify(value.request_defaults, null, 2));
    setMessage(""); setShowKey(false); setFoundModels([]); setModal("source");
  };
  useEffect(() => { void refresh().then(items => {
    if (params.get("model") === "local") { setModal("native"); return; }
    const id = params.get("edit");
    if (id) { const item = items.find(s => s.id === id); if (item) edit(item); }
    else if (params.get("preset") === "zai") edit(items.find(s => s.provider === "zai") || newSource());
  }); }, [refresh]);
  const close = () => { if (busy) return; setModal(null); setMessage(""); if (params.has("edit") || params.has("preset") || params.has("model")) setParams({}); };
  const perform = async (task: () => Promise<void>) => {
    setBusy(true); setMessage("");
    try { await task(); } catch (e) { setMessage((e as Error).message); } finally { setBusy(false); }
  };
  const payload = () => ({ ...draft, models: [...new Set(modelText.split(/[\n,]/).map(v => v.trim()).filter(Boolean))], request_defaults: JSON.parse(defaultsText) });
  const save = async (test = false) => perform(async () => {
    const saved = await sourceRequest<AISource>(`/${draft.id}`, payload(), "PUT");
    setDraft(saved);
    if (test) {
      const result = await sourceRequest<{ source: AISource }>(`/${saved.id}/test`, {});
      setDraft(result.source); setMessage(tr("连接成功，已保存。现在可以在 AI 工作台和本机 API 中使用。"));
    } else { setModal(null); setParams({}); notify("ok", saved.status === "needs_key" ? tr("已保存，等待填写密钥") : tr("来源已保存")); }
    await refresh();
  });
  const currentCLI = cli?.services.find(s => s.kind === kind);
  const visible = sources.filter(s => `${s.name} ${s.models.join(" ")}`.toLowerCase().includes(query.toLowerCase()));
  const hasNative = native && (native.installed || native.state !== "not_installed");
  const isEmpty = !loading && !error && !sources.length && !hasNative && !cli?.services.some(s => s.configured) && !peers.length;
  const href = (path: string) => personalHref(path, client.mode === "fixture");
  const openCLI = () => { setKind("codex_cli"); setMessage(""); setModal("cli"); };
  const addSourceButton = <button className="ais-primary" onClick={() => { setMessage(""); setModal("choose"); }}><Plus size={17} /> {tr("添加来源")}</button>;
  return <div className="ais-page">
    <header className="ais-header"><div><h1>{tr("AI 来源")}</h1><p>{tr("连接模型、厂商 API 与本地 CLI。")}</p></div>{!isEmpty && addSourceButton}</header>
    {isEmpty && <section className="ais-onboarding" aria-labelledby="ais-connect-title">
      <div className="ais-onboarding-heading"><div>
      <h2 id="ais-connect-title">{tr("连接你的第一个 AI 来源")}</h2>
      <p>{tr("选择已有服务，连接后即可在 Ryn 中使用。")}</p>
      </div>{addSourceButton}</div>
      <div className="ais-connect-options">
        <button onClick={() => edit(newSource())}><span className="ais-connect-icon"><Cloud size={28} /></span><strong>{tr("厂商 API")}</strong><span>{tr("填写厂商密钥或自定义地址")}</span><span className="ais-connect-action">{tr("开始连接")} <ArrowRight size={17} /></span></button>
        <button onClick={() => setModal("native")}><span className="ais-connect-icon"><Server size={28} /></span><strong>{tr("本地模型")}</strong><span>{tr("连接 Ollama、LM Studio 或 GGUF 模型")}</span><span className="ais-connect-action">{tr("开始连接")} <ArrowRight size={17} /></span></button>
        <button onClick={openCLI}><span className="ais-connect-icon"><Terminal size={28} /></span><strong>{tr("本地 CLI")}</strong><span>{tr("使用 Codex、Claude Code 登录")}</span><span className="ais-connect-action">{tr("开始连接")} <ArrowRight size={17} /></span></button>
      </div>
    </section>}
    {!isEmpty && <div className="ais-toolbar"><div className="ais-search"><Search size={17} /><input aria-label={tr("搜索来源")} placeholder={tr("搜索来源或模型…")} value={query} onChange={e => setQuery(e.target.value)} /><button aria-label={tr("刷新来源")} onClick={() => void refresh()}><RefreshCw size={16} /></button></div><WorkspaceSelect label={tr("设备筛选")} caption={tr("设备")} value={filter} options={[{ value: "all", label: tr("全部设备") }, { value: "local", label: tr("当前设备") }, { value: "remote", label: tr("其他设备") }]} onChange={setFilter} /></div>}
    {error && <div role="alert" className="ais-error">{error}<button onClick={() => void refresh()}>{tr("重试")}</button></div>}
    {loading ? <p role="status">{tr("正在读取来源…")}</p> : !isEmpty && <>
      {filter !== "remote" && <section className="ais-group"><h2><Laptop size={18} /> {tr("本机 ·")} {device}</h2>
        {native && (native.installed || native.state !== "not_installed") && native.name.toLowerCase().includes(query.toLowerCase()) && <div className="ais-row">
          <span className="ais-brand"><Box size={24} /></span><button className="ais-row-name" onClick={() => setModal("native")}><strong>{native.name || tr("本地模型")}</strong><small>{tr("本地模型 · Ryn 管理")}{native.sharing ? tr(" · 已分享") : ""}</small></button>
          <span className={`ais-status ${native.state === "running" ? "ready" : ""}`}>{nativeState(native)}</span>
          <button className="ais-secondary" disabled={busy || nativeBusy(native)} onClick={() => void perform(async () => {
            if (!native.installed) { setModal("native"); return; }
            try { setNative(await nativeModelRequest(native.state === "running" ? "stop" : "start", {})); }
            catch (e) { notify("danger", nativeError((e as Error).message)); }
          })}>{native.state === "running" ? tr("停止") : nativeBusy(native) ? tr("处理中…") : !native.installed ? tr("选择模型文件") : native.state === "error" ? tr("重试") : tr("启动")}</button>
          <button className="ais-icon" aria-label={tr("本地模型设置")} onClick={() => setModal("native")}><Settings2 size={16} /></button>
        </div>}
        {visible.map(s => <div className="ais-row" key={s.id}><Brand provider={s.provider} /><button className="ais-row-name" onClick={() => edit(s)}><strong>{s.name}</strong><small>{s.kind === "api" ? "API" : tr("本地模型")} · {s.models.length} {tr("个模型")}{s.sharing ? tr(" · 已分享") : ""}</small></button><span className={`ais-status ${s.enabled && s.status === "ready" ? "ready" : ""}`}>{statusLabel(s)}</span><button className="ais-icon" aria-label={tr("编辑 {{v0}}", { v0: s.name })} onClick={() => edit(s)}><Pencil size={15} /></button></div>)}
        {cli?.services.filter(s => s.configured && `${s.title} ${s.kind === "codex_cli" ? "ChatGPT" : "Claude"}`.toLowerCase().includes(query.toLowerCase())).map(s => <div className="ais-row" key={s.kind}><Brand provider={s.kind} /><button className="ais-row-name" onClick={() => { setKind(s.kind); setMessage(""); setModal("cli"); }}><strong>{s.kind === "codex_cli" ? "ChatGPT" : "Claude"}</strong><small>CLI · {s.title}{s.publication_enabled ? tr(" · 已分享") : ""}</small></button><span className={`ais-status ${s.online ? "ready" : ""}`}>{s.online ? tr("可用") : tr("不可用")}</span><ChevronRight size={16} /></div>)}
        {!visible.length && !native?.installed && !cli?.services.some(s => s.configured) && <p className="ais-empty">{tr("添加一个 API、CLI 或本地模型，开始使用。")}</p>}
      </section>}
      {filter !== "local" && peers.filter(s => s.service.model_alias.toLowerCase().includes(query.toLowerCase())).map(s => <section className="ais-group" key={`${s.peer_id}:${s.service.package_id}`}><h2><Monitor size={18} /> {s.node_name || s.peer_id}</h2><div className="ais-row"><Brand provider={s.service.adapter || "custom"} /><div className="ais-row-name"><strong>{s.service.model_alias}</strong><small>{tr("其他节点分享 · 在所属设备上编辑配置")}</small></div><Link to={href(`/services/private-ai/chat?peer=${encodeURIComponent(s.peer_id)}&service=${encodeURIComponent(s.service.package_id)}`)}>{tr("使用")} <ChevronRight size={14} /></Link></div></section>)}
    </>}
    <aside className="ais-api-callout"><span className="ais-connect-icon"><Code2 size={25} /></span><div><h2>{tr("想让其他程序调用 AI？")}</h2><p>{tr("API 接入提供本机调用地址、模型名称和项目密钥。")}</p></div><Link className="ais-secondary" to={href("/services/api")}>{tr("查看 API 接入")} <ArrowRight size={16} /></Link></aside>
    <footer className="ais-links"><Link to={href("/services/agent-sharing")}><Share2 size={14} /> {tr("管理共享")}</Link><Link to={href("/services/model-mapping")}>{tr("模型映射")}</Link>{isEmpty && <button className="ais-refresh-empty" onClick={() => void refresh()}><RefreshCw size={14} />{tr("刷新来源")}</button>}</footer>
    {modal && <Modal title={modal === "native" ? tr("本地模型") : modal === "choose" ? tr("添加来源") : modal === "cli" ? tr("连接本地 CLI") : draft.id === "new" ? tr("添加来源") : tr("编辑来源")} onClose={close}>
      <div className="ais-modal">
      {modal === "choose" && <><p className="ais-muted">{tr("选择一种接入方式")}</p><div className="ais-choices">
        <button onClick={() => edit(newSource())}><Cloud /><span><strong>{tr("厂商 API")}</strong><small>{tr("预设厂商，或填写自定义地址")}</small></span><ChevronRight /></button>
        <button onClick={() => { setKind("codex_cli"); setModal("cli"); }}><Terminal /><span><strong>{tr("本地 CLI")}</strong><small>{tr("连接 Codex、Claude Code")}</small></span><ChevronRight /></button>
        <button onClick={() => setModal("native")}><Box /><span><strong>{tr("本地模型")}</strong><small>{tr("选择你已有的 GGUF 模型文件")}</small></span><ChevronRight /></button>
      </div></>}
      {modal === "native" && <>{nativeLoadError && <p role="alert">{nativeLoadError}</p>}<NativeModelPanel status={native} onUpdate={setNative} onConnect={() => edit(newSource("ollama"))} /></>}
      {modal === "source" && <>
        <WorkspaceSelect caption={draft.kind === "api" ? tr("厂商") : tr("本地服务")} label={tr("厂商")} value={draft.provider} icon={<Brand provider={draft.provider} />} options={sourcePresets.filter(p => draft.kind === "local" ? ["ollama", "lmstudio", "custom"].includes(p.id) : !["ollama", "lmstudio"].includes(p.id)).map(p => ({ value: p.id, label: p.name }))} disabled={busy} onChange={provider => { const next = newSource(provider); edit({ ...next, id: draft.id, kind: draft.kind, has_key: false, clear_key: true, sharing: draft.sharing }); }} />
        <p className="ais-muted ais-protocol">{tr("OpenAI 兼容 Chat Completions")}</p>
        <label className="ais-field">{tr("API Key")} {draft.kind === "local" && <small>{tr("（可选）")}</small>}<span className="ais-secret"><input aria-label={tr("API Key")} type={showKey ? "text" : "password"} autoComplete="off" spellCheck={false} value={draft.api_key || ""} placeholder={draft.has_key ? tr("已保存 · 留空保留原密钥") : tr("粘贴你的 API Key")} onChange={e => setDraft({ ...draft, api_key: e.target.value, clear_key: false })} /><button type="button" aria-label={showKey ? tr("隐藏密钥") : tr("显示密钥")} onClick={() => setShowKey(!showKey)}>{showKey ? <EyeOff size={16} /> : <Eye size={16} />}</button></span></label>
        {draft.has_key && <label className="ais-check"><input type="checkbox" checked={draft.clear_key || false} onChange={e => setDraft({ ...draft, clear_key: e.target.checked })} /> {tr("清除已保存的密钥")}</label>}
        <label className="ais-field">{tr("来源名称")}<input value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} /></label>
        <div className="ais-device"><span>{tr("配置设备")}</span><span><Laptop size={16} /> {device} {tr("· 本机")}</span></div>
        <details className="ais-disclosure" open={draft.provider === "custom" || draft.kind === "local" || undefined}><summary><Settings2 size={17} /> {tr("连接设置")} <small>{tr("地址与请求参数")}</small></summary>
          <label className="ais-field">{tr("服务地址")}<input aria-label={tr("服务地址")} value={draft.base_url} placeholder="https://example.com/v1" onChange={e => setDraft({ ...draft, base_url: e.target.value })} /></label>
          <p className="ais-muted">{tr("填写接口根地址，系统会追加 /chat/completions。")}</p>
          <label className="ais-field">{tr("请求参数（JSON）")}<textarea rows={4} value={defaultsText} onChange={e => setDefaultsText(e.target.value)} /></label>
        </details>
        <details className="ais-disclosure" open={!draft.models.length || undefined}><summary><Box size={17} /> {tr("模型")} <small>{modelText.split(/[\n,]/).filter(Boolean).length} {tr("个 · 管理")}</small></summary>
          <label className="ais-field">{tr("模型 ID（每行一个）")}<textarea aria-label={tr("模型 ID")} rows={3} value={modelText} onChange={e => setModelText(e.target.value)} /></label>
          <button className="ais-secondary" disabled={busy} onClick={() => void perform(async () => { const result = await sourceRequest<{ models: string[] }>("/models", payload()); setFoundModels(result.models); setMessage(result.models.length ? tr("已读取模型，点击名称添加") : tr("未返回模型，请手动填写 ID")); })}><RefreshCw size={14} /> {tr("读取模型")}</button>
          {!!foundModels.length && <div className="ais-models">{foundModels.map(m => <button key={m} onClick={() => setModelText([...new Set([...modelText.split(/\n/).filter(Boolean), m])].join("\n"))}>{m}</button>)}</div>}
          <div className="ais-limits"><label className="ais-field">{tr("上下文窗口")}<input type="number" min={256} max={2000000} value={draft.context_window} onChange={e => setDraft({ ...draft, context_window: Number(e.target.value) })} /></label><label className="ais-field">{tr("最大输出 Token")}<input type="number" min={1} max={256000} value={draft.max_output_tokens} onChange={e => setDraft({ ...draft, max_output_tokens: Number(e.target.value) })} /></label></div>
          <p className="ais-muted">{tr("当前工作台支持文本；图片、视频、PDF 不会因厂商支持而自动启用。")}</p>
        </details>
        <details className="ais-disclosure"><summary><Share2 size={17} /> {tr("分享给其他设备")} <small>{draft.sharing ? tr("已开启") : tr("未开启")}</small></summary><label className="ais-check"><input type="checkbox" checked={draft.sharing} onChange={e => setDraft({ ...draft, sharing: e.target.checked })} /> {tr("分享给我的个人空间节点")}</label><p className="ais-muted">{tr("凭据保留在本机。需先在个人空间设置中启用 AI 访问。")}</p></details>
        <label className="ais-check"><input type="checkbox" checked={draft.enabled} onChange={e => setDraft({ ...draft, enabled: e.target.checked })} /><Power size={14} /> {tr("启用这个来源")}</label>
        <p className="ais-privacy"><ShieldCheck size={14} /> {tr("密钥由当前节点加密保存，不返回页面。")}</p>
      </>}
      {modal === "cli" && <>
        <WorkspaceSelect caption={tr("CLI 工具")} label={tr("CLI 工具")} value={kind} icon={<Brand provider={kind} />} options={[{ value: "codex_cli", label: "ChatGPT · Codex CLI" }, { value: "claude_cli", label: "Claude · Claude Code" }]} onChange={value => setKind(value as typeof kind)} disabled={busy} />
        <div className="ais-device"><span>{tr("配置设备")}</span><span><Laptop size={16} /> {device} {tr("· 本机")}</span></div>
        <div className="ais-cli-state"><Check size={18} /><div><strong>{currentCLI?.installed ? tr("已检测到程序") : tr("尚未检测到程序")}</strong><p>{currentCLI?.configured ? tr("已配置，连接测试已通过") : tr("安装并登录后，点击下方“配置并验证”")}</p></div><button className="ais-icon" aria-label={tr("重新检测 CLI")} disabled={busy} onClick={() => void perform(async () => { await refresh(); })}><RefreshCw size={16} /></button></div>
        <p className="ais-muted">{kind === "codex_cli" ? tr("如需登录，请在终端运行 codex login。") : tr("如需登录，请在终端启动 claude 完成登录。")} {tr("当前使用已登录的系统用户环境。")}</p>
        <details className="ais-disclosure"><summary><Share2 size={17} /> {tr("分享给其他设备")} <small>{currentCLI?.publication_enabled ? tr("已开启") : tr("未开启")}</small></summary><button className="ais-secondary" disabled={busy || !currentCLI?.configured} onClick={() => void perform(async () => { await client.setCLISharing(kind, !currentCLI?.publication_enabled); await refresh(); })}>{currentCLI?.publication_enabled ? tr("停止分享") : tr("分享给我的节点")}</button></details>
        <p className="ais-privacy"><ShieldCheck size={14} /> {tr("CLI 在本机运行，内容可能发送给模型服务商。")}</p>
      </>}
      {message && <p role="status" className="ais-feedback">{message}</p>}
      {modal !== "choose" && modal !== "native" && <div className="ais-modal-footer">
        {modal === "source" ? <><button className="ais-secondary" disabled={busy} onClick={() => void save(true)}>{busy ? tr("处理中…") : tr("保存并测试")}</button><div className="ais-footer-right">{draft.id !== "new" && <button className="ais-icon" aria-label={tr("删除来源")} disabled={busy} onClick={() => confirm({ title: tr("删除这个 AI 来源？"), body: tr("将删除当前节点上的配置与密钥，不影响厂商账号。"), risk: "high", confirmLabel: tr("删除来源"), onConfirm: async () => { await sourceRequest(`/${draft.id}`, undefined, "DELETE"); setModal(null); setParams({}); await refresh(); } })}><Trash2 size={17} /></button>}<button className="ais-primary" disabled={busy || !draft.name.trim()} onClick={() => void save()}>{tr("保存")}</button></div></> : <><button className="ais-secondary" disabled={busy} onClick={close}>{tr("关闭")}</button><button className="ais-primary" disabled={busy || !currentCLI?.installed} onClick={() => void perform(async () => { await client.setupCLIService(kind); await refresh(); setMessage(tr("连接成功，可以在 AI 工作台中选择。")); })}>{busy ? tr("正在验证…") : currentCLI?.configured ? tr("重新验证") : tr("配置并验证")}</button></>}
      </div>}
      </div>
    </Modal>}
  </div>;
}
