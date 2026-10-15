import { tr, useUILanguage } from "../uiI18n";
import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Cloud, Terminal, Box, Plus, ChevronRight, Laptop, RefreshCw, Check, Settings2, Share2, Eye, EyeOff, ShieldCheck, Trash2, Power, ArrowRight, Server } from "lucide-react";
import { useAppContext } from "../appContext";
import { Modal } from "../personal/components";
import { newSource, sourcePresets, sourceRequest, type AISource } from "../domain/aiSources";
import type { CLIServicesStatus, CLIServiceStatus, LLMServiceRecord } from "../domain/nodeClient";
import WorkspaceSelect from "./WorkspaceSelect";
import "./AISources.css";
import NativeModelPanel from "./components/NativeModelPanel";
import ModelPool from "./ModelPool";
import { nativeModelRequest, type NativeModelStatus } from "../domain/nativeModel";
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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [modal, setModal] = useState<"choose" | "source" | "cli" | "native" | "manage" | null>(null);
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
  const payload = () => {
    const models = [...new Set(modelText.split(/[\n,]/).map(v => v.trim()).filter(Boolean))];
    const shared = draft.shared_models ?? (draft.sharing ? draft.models : []);
    return { ...draft, models, shared_models: shared.filter(model => models.includes(model)), request_defaults: JSON.parse(defaultsText) };
  };
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
  const hasNative = native && (native.installed || native.state !== "not_installed");
  const isEmpty = !loading && !error && !sources.length && !hasNative && !cli?.services.some(s => s.configured) && !peers.length;
  const href = (path: string) => personalHref(path, client.mode === "fixture");
  const openCLI = () => { setKind("codex_cli"); setMessage(""); setModal("cli"); };
  const addSourceButton = <button className="ais-primary" onClick={() => { setMessage(""); setModal("choose"); }}><Plus size={17} /> {tr("添加模型")}</button>;
  return <div className="ais-page">
    <header className="ais-header"><div><h1>{tr("模型")}</h1><p>{tr("接入一次，自己使用，也可以分享给其他设备。")}</p></div><div className="ais-header-actions"><button className="ais-secondary" onClick={() => setModal("manage")}>{tr("管理来源")}</button>{!isEmpty && addSourceButton}</div></header>
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
    <ModelPool sources={sources} cli={cli} native={native} peers={peers} loading={loading} error={error} onRefresh={refresh}
      onEditSource={edit} onCLI={value => { setKind(value); setMessage(""); setModal("cli"); }} onNative={() => setModal("native")} />
    <footer className="ais-links"><Link to={href("/services/api")}>{tr("查看 API 接入")}</Link><Link to={href("/services/agent-sharing")}><Share2 size={14} /> {tr("管理共享")}</Link><Link to={href("/services/model-mapping")}>{tr("模型映射")}</Link>{isEmpty && <button className="ais-refresh-empty" onClick={() => void refresh()}><RefreshCw size={14} />{tr("刷新来源")}</button>}</footer>
    {modal && <Modal title={modal === "native" ? tr("本地模型") : modal === "manage" ? tr("管理来源") : modal === "choose" ? tr("添加模型") : modal === "cli" ? tr("连接本地 CLI") : draft.id === "new" ? tr("添加来源") : tr("编辑来源")} onClose={close}>
      <div className="ais-modal">
      {modal === "manage" && <><p className="ais-muted">{tr("一个来源可以提供多个模型，模型分享单独设置。")}</p><div className="ais-source-manager">{sources.map(source => <button key={source.id} onClick={() => edit(source)}><span><strong>{source.name}</strong><small>{source.models.length} {tr("个模型")} · {statusLabel(source)}</small></span><Settings2 size={16} /></button>)}</div><button className="ais-primary" onClick={() => setModal("choose")}><Plus size={16} />{tr("添加来源")}</button></>}
      {modal === "choose" && <>{sources.length > 0 && <div className="ais-source-manager"><h3>{tr("从已有来源添加模型")}</h3>{sources.map(source => <button key={source.id} onClick={() => edit(source)}><span><strong>{source.name}</strong><small>{tr("修改模型列表或读取可用模型")}</small></span><Plus size={16} /></button>)}</div>}<p className="ais-muted">{tr("选择一种接入方式")}</p><div className="ais-choices">
        <button onClick={() => edit(newSource())}><Cloud /><span><strong>{tr("厂商 API")}</strong><small>{tr("预设厂商，或填写自定义地址")}</small></span><ChevronRight /></button>
        <button onClick={() => { setKind("codex_cli"); setModal("cli"); }}><Terminal /><span><strong>{tr("本地 CLI")}</strong><small>{tr("连接 Codex、Claude Code、Antigravity")}</small></span><ChevronRight /></button>
        <button onClick={() => setModal("native")}><Box /><span><strong>{tr("本地模型")}</strong><small>{tr("选择你已有的 GGUF 模型文件")}</small></span><ChevronRight /></button>
      </div></>}
      {modal === "native" && <>{nativeLoadError && <p role="alert">{nativeLoadError}</p>}<NativeModelPanel status={native} onUpdate={setNative} onConnect={() => edit(newSource("ollama"))} /></>}
      {modal === "source" && <>
        <WorkspaceSelect caption={draft.kind === "api" ? tr("厂商") : tr("本地服务")} label={tr("厂商")} value={draft.provider} icon={<Brand provider={draft.provider} />} options={sourcePresets.filter(p => draft.kind === "local" ? ["ollama", "lmstudio", "custom"].includes(p.id) : !["ollama", "lmstudio"].includes(p.id)).map(p => ({ value: p.id, label: p.name }))} disabled={busy} onChange={provider => { const next = newSource(provider); edit({ ...next, id: draft.id, kind: draft.kind, has_key: false, clear_key: true, sharing: false, shared_models: [] }); }} />
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
        <details className="ais-disclosure"><summary><Share2 size={17} /> {tr("逐模型分享")} <small>{tr("个人空间节点")}</small></summary>
          {[...new Set(modelText.split(/[\n,]/).map(model => model.trim()).filter(Boolean))].map(model => <label className="ais-check" key={model}><input type="checkbox" checked={(draft.shared_models ?? (draft.sharing ? draft.models : [])).includes(model)} onChange={event => { const selected = new Set(draft.shared_models ?? (draft.sharing ? draft.models : [])); if (event.target.checked) selected.add(model); else selected.delete(model); setDraft({ ...draft, shared_models: [...selected] }); }} />{model}</label>)}
          <p className="ais-muted">{tr("新增模型默认不分享。凭据保留在本机，调用方只能使用已分享的模型。")}</p>
        </details>
        <label className="ais-check"><input type="checkbox" checked={draft.enabled} onChange={e => setDraft({ ...draft, enabled: e.target.checked })} /><Power size={14} /> {tr("启用这个来源")}</label>
        <p className="ais-privacy"><ShieldCheck size={14} /> {tr("密钥由当前节点加密保存，不返回页面。")}</p>
      </>}
      {modal === "cli" && <>
        <WorkspaceSelect caption={tr("CLI 工具")} label={tr("CLI 工具")} value={kind} icon={<Brand provider={kind} />} options={[{ value: "codex_cli", label: "ChatGPT · Codex CLI" }, { value: "claude_cli", label: "Claude · Claude Code" }, { value: "antigravity_cli", label: "Gemini · Antigravity" }]} onChange={value => setKind(value as typeof kind)} disabled={busy} />
        <div className="ais-device"><span>{tr("配置设备")}</span><span><Laptop size={16} /> {device} {tr("· 本机")}</span></div>
        <div className="ais-cli-state"><Check size={18} /><div><strong>{currentCLI?.installed ? tr("已检测到程序") : tr("尚未检测到程序")}</strong><p>{currentCLI?.configured ? tr("已配置，可在模型列表中选择") : tr("安装并登录后，点击下方“配置接入”")}</p></div><button className="ais-icon" aria-label={tr("重新检测 CLI")} disabled={busy} onClick={() => void perform(async () => { await refresh(); })}><RefreshCw size={16} /></button></div>
        <p className="ais-muted">{kind === "codex_cli" ? tr("如需登录，请在终端运行 codex login。") : kind === "antigravity_cli" ? tr("使用 Antigravity 官方调用程序。首次连接请在终端启动 agy 完成登录；无需填写 API 地址或密钥。") : tr("如需登录，请在终端启动 claude 完成登录。")} {tr("当前使用已登录的系统用户环境。")}</p>
        <details className="ais-disclosure"><summary><Share2 size={17} /> {tr("分享给其他设备")} <small>{currentCLI?.publication_enabled ? tr("已开启") : tr("未开启")}</small></summary><button className="ais-secondary" disabled={busy || !currentCLI?.configured} onClick={() => void perform(async () => { await client.setCLISharing(kind, !currentCLI?.publication_enabled); await refresh(); })}>{currentCLI?.publication_enabled ? tr("停止分享") : tr("分享给我的节点")}</button></details>
        <p className="ais-privacy"><ShieldCheck size={14} /> {tr("CLI 在本机运行，内容可能发送给模型服务商。")}</p>
      </>}
      {message && <p role="status" className="ais-feedback">{message}</p>}
      {modal !== "choose" && modal !== "native" && modal !== "manage" && <div className="ais-modal-footer">
        {modal === "source" ? <><button className="ais-secondary" disabled={busy} onClick={() => void save(true)}>{busy ? tr("处理中…") : tr("保存并测试")}</button><div className="ais-footer-right">{draft.id !== "new" && <button className="ais-icon" aria-label={tr("删除来源")} disabled={busy} onClick={() => confirm({ title: tr("删除这个 AI 来源？"), body: tr("将删除当前节点上的配置与密钥，不影响厂商账号。"), risk: "high", confirmLabel: tr("删除来源"), onConfirm: async () => { await sourceRequest(`/${draft.id}`, undefined, "DELETE"); setModal(null); setParams({}); await refresh(); } })}><Trash2 size={17} /></button>}<button className="ais-primary" disabled={busy || !draft.name.trim()} onClick={() => void save()}>{tr("保存")}</button></div></> : <><button className="ais-secondary" disabled={busy} onClick={close}>{tr("关闭")}</button><button className="ais-primary" disabled={busy || !currentCLI?.installed} onClick={() => void perform(async () => { const result = await client.setupCLIService(kind); await refresh(); setMessage(tr(result.agent_usable ? "连接已完成，可直接聊天或用于助手。" : "连接已完成，此接入支持聊天，不支持助手工具。")); })}>{busy ? tr("正在接入…") : currentCLI?.configured ? tr("重新接入") : tr("配置接入")}</button></>}
      </div>}
      </div>
    </Modal>}
  </div>;
}
