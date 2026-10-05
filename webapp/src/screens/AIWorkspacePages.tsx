import { tr, useUILanguage, uiLocale } from "../uiI18n";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Check, ChevronRight, CircleHelp, Cloud, Code2, Copy, KeyRound, Laptop, Link2, LockKeyhole, Monitor, Plus, RefreshCw, Route, ShieldCheck, Sparkles, Terminal, Trash2 } from "lucide-react";
import { useAppContext } from "../appContext";
import type { CLIServiceStatus, CLIServicesStatus, InferenceAccess } from "../domain/nodeClient";
import { personalHref } from "../personal/model";
import "./AIWorkspacePages.css";

type Target = InferenceAccess["targets"][number];
const aliasPattern = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$/;

function Header({ icon: Icon, title, description }: { icon: typeof Sparkles; title: string; description: string }) {
  useUILanguage();
  return <div className="aiw-header"><div className="aiw-header-icon"><Icon size={28} strokeWidth={1.7} /></div><div><h1>{title}</h1><p>{description}</p></div></div>;
}

function Badge({ children, tone = "neutral" }: { children: React.ReactNode; tone?: "neutral" | "success" | "warning" }) {
  useUILanguage();
  return <span className={`aiw-badge aiw-badge-${tone}`}>{children}</span>;
}

function Loading() {
  useUILanguage(); return <div className="aiw-state"><RefreshCw size={20} className="aiw-spin" />{tr("正在读取此节点的配置…")}</div>; }
function Failure({ message, retry }: { message: string; retry: () => void }) {
  useUILanguage(); return <div className="aiw-state aiw-error"><span>{message}</span><button className="aiw-button" onClick={retry}>{tr("重试")}</button></div>; }

function useAccess() {
  const { client } = useAppContext();
  const [access, setAccess] = useState<InferenceAccess | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const refresh = useCallback(async () => {
    setLoading(true);
    try { setAccess(await client.getInferenceAccess()); setError(""); }
    catch (reason) { setError(reason instanceof Error ? reason.message : tr("无法读取 API 配置")); }
    finally { setLoading(false); }
  }, [client]);
  useEffect(() => { void refresh(); }, [refresh]);
  return { access, loading, error, refresh };
}

function labelFor(target: Target) {
  const source = target.rynmesh.source === "peer" ? tr("远端节点") : tr("本机节点");
  return `${target.rynmesh.model_alias} · ${source}`;
}

function targetKind(target?: Target) {
  if (!target) return tr("目标离线");
  if (target.rynmesh.adapter === "codex_cli") return "Codex CLI";
  if (target.rynmesh.adapter === "claude_cli") return "Claude Code";
  return tr("本地模型");
}

async function copyText(value: string, onSuccess: () => void) { await navigator.clipboard.writeText(value); onSuccess(); }

export function APIAccessPage() {
  useUILanguage();
  const { client, node, notify } = useAppContext();
  const { access, loading, error, refresh } = useAccess();
  const [protocol, setProtocol] = useState<"openai" | "anthropic">("openai");
  const [project, setProject] = useState(tr("我的项目"));
  const [limit, setLimit] = useState(100000);
  const [freshKey, setFreshKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [selectedModel, setSelectedModel] = useState("");
  const model = access?.models.find(item => item.id === selectedModel) || access?.models[0];
  const base = protocol === "openai" ? access?.base_url || "http://127.0.0.1:8791/v1" : (access?.base_url || "http://127.0.0.1:8791/v1").replace(/\/v1$/, "");
  const sample = protocol === "openai"
    ? `from openai import OpenAI\n\nclient = OpenAI(base_url="${base}", api_key="YOUR_RYNMESH_KEY")\nresponse = client.chat.completions.create(\n    model="${model?.id || "MODEL_ID"}",\n    messages=[{"role": "user", "content": ${JSON.stringify(tr("Hello"))}}],\n)\nprint(response.choices[0].message.content)`
    : `from anthropic import Anthropic\n\nclient = Anthropic(base_url="${base}", api_key="YOUR_RYNMESH_KEY")\nmessage = client.messages.create(\n    model="${model?.id || "MODEL_ID"}",\n    max_tokens=256,\n    messages=[{"role": "user", "content": ${JSON.stringify(tr("Hello"))}}],\n)\nprint(message.content[0].text)`;
  async function createKey() {
    if (!project.trim() || !Number.isFinite(limit) || limit < 1) return;
    setBusy(true);
    try { const result = await client.createInferenceKey(project.trim(), limit); setFreshKey(result.key); await refresh(); notify("ok", tr("项目密钥已创建。请现在保存，关闭后无法再次查看。")); }
    catch (reason) { notify("danger", reason instanceof Error ? reason.message : tr("创建密钥失败")); }
    finally { setBusy(false); }
  }
  async function revokeKey(id: string) {
    setBusy(true);
    try { await client.revokeInferenceKey(id); setFreshKey(""); await refresh(); notify("ok", tr("密钥已撤销")); }
    catch (reason) { notify("danger", reason instanceof Error ? reason.message : tr("撤销失败")); }
    finally { setBusy(false); }
  }
  const copied = () => notify("ok", tr("已复制到剪贴板"));
  return <div className="aiw-page">
    <Header icon={Link2} title={tr("API 接入")} description={tr("让笔记本上的其他程序，通过本机节点调用家里电脑的 AI 服务。")} />
    <div className="aiw-flow"><span><Laptop size={17} /> {tr("当前设备 ·")} {node.node_name}</span><ChevronRight size={16} /><span><ShieldCheck size={17} /> {tr("Ryn 加密连接")}</span><ChevronRight size={16} /><span><Monitor size={17} /> {tr("家里节点执行")}</span></div>
    {loading && !access ? <Loading /> : error && !access ? <Failure message={error} retry={() => void refresh()} /> : access && <>
      <section className="aiw-card aiw-endpoint"><div className="aiw-section-heading"><div><span className="aiw-eyebrow">{tr("LOCAL ENDPOINT")}</span><h2>{tr("本机 API 地址")}</h2><p>{tr("其他程序只需连接笔记本上的节点地址，节点会负责路由和格式转换。")}</p></div><Badge tone="success">{tr("仅本机监听")}</Badge></div>
        <div className="aiw-tabs"><button className={protocol === "openai" ? "active" : ""} onClick={() => setProtocol("openai")}>{tr("OpenAI 兼容")}</button><button className={protocol === "anthropic" ? "active" : ""} onClick={() => setProtocol("anthropic")}>{tr("Anthropic 兼容")}</button></div>
        <div className="aiw-copy-row"><code>{base}</code><button aria-label={tr("复制 API 地址")} onClick={() => void copyText(base, copied)}><Copy size={17} /> {tr("复制地址")}</button></div>
        <div className="aiw-hint"><LockKeyhole size={16} /> {tr("API 不提供文件或命令参数；CLI 仍以家里电脑的登录用户运行，请只分享给可信任的个人节点。")}</div>
      </section>
      <div className="aiw-two-col"><section className="aiw-card"><div className="aiw-section-heading"><div><span className="aiw-eyebrow">{tr("MODEL")}</span><h2>{tr("调用模型")}</h2></div><Link to={personalHref("/services/model-mapping", client.mode === "fixture")} className="aiw-text-link">{tr("管理映射")} <ArrowRight size={15} /></Link></div>
        <label className="aiw-label">{tr("模型名称")}<select value={model?.id || ""} onChange={event => setSelectedModel(event.target.value)}>{access.models.map(item => <option value={item.id} key={item.id}>{item.id}</option>)}</select></label>
        {model ? <div className="aiw-model-info"><span>{targetKind(model)} · {model.rynmesh.source === "peer" ? tr("远端节点") : tr("本机节点")}</span><Badge tone="success">{tr("可调用")}</Badge></div> : <p className="aiw-empty">{tr("暂无可调用模型。请先连接并发布家里的服务。")}</p>}
      </section><section className="aiw-card"><div className="aiw-section-heading"><div><span className="aiw-eyebrow">{tr("PROJECT KEY")}</span><h2>{tr("创建项目密钥")}</h2></div><KeyRound size={19} /></div>
        <div className="aiw-form-row"><label className="aiw-label">{tr("项目名称")}<input value={project} onChange={event => setProject(event.target.value)} maxLength={80} /></label><label className="aiw-label">{tr("输出 Token 额度")}<input type="number" min={1} max={1000000000} value={limit} onChange={event => setLimit(Number(event.target.value))} /></label></div>
        <button className="aiw-primary" disabled={busy || !project.trim() || limit < 1} onClick={() => void createKey()}><Plus size={16} /> {tr("创建密钥")}</button>
        {freshKey && <div className="aiw-secret"><strong>{tr("仅显示这一次，请妥善保存")}</strong><div className="aiw-copy-row"><code>{freshKey}</code><button onClick={() => void copyText(freshKey, copied)}><Copy size={16} /> {tr("复制")}</button></div><button className="aiw-plain" onClick={() => setFreshKey("")}>{tr("我已保存，隐藏密钥")}</button></div>}
      </section></div>
      <section className="aiw-card"><div className="aiw-section-heading"><div><span className="aiw-eyebrow">{tr("KEYS")}</span><h2>{tr("已有项目密钥")}</h2></div><Badge>{access.keys.filter(key => !key.revoked).length} {tr("个有效")}</Badge></div>
        {access.keys.length ? <div className="aiw-key-list">{access.keys.map(key => <div className="aiw-key" key={key.id}><KeyRound size={19} /><div><strong>{key.name}</strong><small>{tr("已使用")} {key.used_output_tokens.toLocaleString(uiLocale())} / {key.output_token_limit.toLocaleString(uiLocale())} {tr("输出 Token")}</small></div><Badge tone={key.revoked ? "neutral" : "success"}>{key.revoked ? tr("已撤销") : tr("有效")}</Badge>{!key.revoked && <button className="aiw-icon" title={tr("撤销密钥")} disabled={busy} onClick={() => void revokeKey(key.id)}><Trash2 size={17} /></button>}</div>)}</div> : <p className="aiw-empty">{tr("还没有项目密钥。创建后即可让本机程序调用模型。")}</p>}
      </section>
      <section className="aiw-card"><div className="aiw-section-heading"><div><span className="aiw-eyebrow">{tr("QUICK START")}</span><h2>{tr("代码示例")}</h2></div><button className="aiw-button" onClick={() => void copyText(sample, copied)}><Copy size={16} /> {tr("复制代码")}</button></div><pre className="aiw-code"><code>{sample}</code></pre><p className="aiw-footnote">{tr("将 YOUR_RYNMESH_KEY 换成上方创建的密钥。远端模型请求使用节点间加密连接。")}</p></section>
    </>}
  </div>;
}

export function ModelMappingPage() {
  useUILanguage();
  const { client, notify } = useAppContext();
  const { access, loading, error, refresh } = useAccess();
  const [name, setName] = useState("");
  const [targetId, setTargetId] = useState("");
  const [saving, setSaving] = useState(false);
  const selected = access?.targets.find(item => item.id === targetId) || access?.targets[0];
  async function save() {
    if (!selected || !aliasPattern.test(name)) return;
    setSaving(true);
    try { await client.setInferenceModelAlias(name, selected.id); await refresh(); notify("ok", tr("模型别名 {{v0}} 已保存", { v0: name })); }
    catch (reason) { notify("danger", reason instanceof Error ? reason.message : tr("保存映射失败")); }
    finally { setSaving(false); }
  }
  return <div className="aiw-page"><Header icon={Route} title={tr("模型映射")} description={tr("给来自不同节点的服务一个稳定名称，让笔记本上的程序无需关心它在哪台电脑运行。")} />
    {loading && !access ? <Loading /> : error && !access ? <Failure message={error} retry={() => void refresh()} /> : access && <>
      <section className="aiw-card"><div className="aiw-section-heading"><div><span className="aiw-eyebrow">{tr("SAVED ROUTES")}</span><h2>{tr("已保存的映射")}</h2></div><Badge>{Object.keys(access.aliases).length} {tr("条")}</Badge></div>
        {Object.entries(access.aliases).length ? <div className="aiw-route-list">{Object.entries(access.aliases).map(([alias, id]) => { const target = access.targets.find(item => item.id === id); return <button className="aiw-route" key={alias} onClick={() => { setName(alias); setTargetId(id); }}><div className="aiw-route-name"><Code2 size={18} /><strong>{alias}</strong></div><ArrowRight size={17} /><div><strong>{target ? labelFor(target) : tr("当前不可用")}</strong><small>{target ? targetKind(target) : id}</small></div><Badge tone={target ? "success" : "warning"}>{target ? tr("可用") : tr("离线")}</Badge><ChevronRight size={16} /></button>; })}</div> : <p className="aiw-empty">{tr("还没有映射。下方选择一个服务并创建别名。")}</p>}
      </section>
      <section className="aiw-card"><div className="aiw-section-heading"><div><span className="aiw-eyebrow">{tr("EDIT ROUTE")}</span><h2>{tr("创建或修改映射")}</h2></div><Route size={20} /></div><div className="aiw-map-grid"><label className="aiw-label">{tr("对外模型名称")}<input placeholder={tr("例如：home-codex")} value={name} onChange={event => setName(event.target.value)} maxLength={64} /></label><ArrowRight className="aiw-map-arrow" size={19} /><label className="aiw-label">{tr("实际服务")}<select value={selected?.id || ""} onChange={event => setTargetId(event.target.value)}>{access.targets.map(item => <option key={item.id} value={item.id}>{labelFor(item)} · {targetKind(item)}</option>)}</select></label></div>
        {selected ? <div className="aiw-target-detail"><strong>{selected.rynmesh.model_alias}</strong><span>{targetKind(selected)} · {selected.rynmesh.source === "peer" ? tr("家里 / 远端节点") : tr("当前节点")}</span><span>{tr("最大输出")} {selected.rynmesh.max_output_tokens.toLocaleString(uiLocale())} Token</span></div> : <p className="aiw-empty">{tr("暂无可用服务。先连接家里的节点或配置本机模型。")}</p>}
        <div className="aiw-actions"><button className="aiw-primary" disabled={saving || !selected || !aliasPattern.test(name)} onClick={() => void save()}><Check size={16} /> {tr("保存映射")}</button><span>{tr("可使用字母、数字、点、短横线和下划线，最长 64 个字符。")}</span></div>
      </section><div className="aiw-note"><CircleHelp size={18} /><p>{tr("映射保存在当前笔记本节点。API 调用中的")} <code>{tr("model")}</code> {tr("使用左侧名称，节点会将请求转到右侧服务，并输出统一的 API 格式。")}</p></div>
    </>}
  </div>;
}

export function AgentSharingPage() {
  useUILanguage();
  const { client, notify } = useAppContext();
  const [state, setState] = useState<CLIServicesStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const refresh = useCallback(async () => { setLoading(true); try { setState(await client.getCLIServices()); setError(""); } catch (reason) { setError(reason instanceof Error ? reason.message : tr("无法读取服务状态")); } finally { setLoading(false); } }, [client]);
  useEffect(() => { void refresh(); }, [refresh]);
  async function setup(service: CLIServiceStatus) { setBusy(service.kind); try { await client.setupCLIService(service.kind); await refresh(); notify("ok", tr("{{v0}} 已配置，可以选择是否分享", { v0: service.title })); } catch (reason) { notify("danger", reason instanceof Error ? reason.message : tr("配置失败")); } finally { setBusy(""); } }
  async function share(service: CLIServiceStatus) { setBusy(service.kind); try { await client.setCLISharing(service.kind, !service.publication_enabled); await refresh(); notify("ok", service.publication_enabled ? tr("已停止分享") : tr("已分享给个人空间中的节点")); } catch (reason) { notify("danger", reason instanceof Error ? reason.message : tr("更新分享失败")); } finally { setBusy(""); } }
  return <div className="aiw-page"><Header icon={Sparkles} title={tr("服务共享")} description={tr("把家里电脑上的 CLI Agent 分享给自己的笔记本节点，再由笔记本提供本机 API。")} />
    <div className="aiw-flow"><span><Monitor size={17} /> {tr("家里电脑上的 CLI")}</span><ChevronRight size={16} /><span><ShieldCheck size={17} /> {tr("个人空间节点")}</span><ChevronRight size={16} /><span><Laptop size={17} /> {tr("笔记本对话 + API")}</span></div>
    {loading && !state ? <Loading /> : error && !state ? <Failure message={error} retry={() => void refresh()} /> : state && <>
      {state.services.some(service => service.configured && service.online) && <div className="aiw-note"><Check size={18} /><p>{tr("本机 CLI 已就绪，当前电脑可以直接使用。")}<Link to={personalHref("/services/private-ai/chat", client.mode === "fixture")}>{tr("打开 AI 工作台")} <ArrowRight size={14} /></Link></p></div>}
      {!state.personal_space_ready && <div className="aiw-note aiw-warning"><LockKeyhole size={18} /><p>{tr("分享 CLI 服务前，请先加入个人空间，并在家里节点开启“个人空间 AI 访问”。")}<Link to={personalHref("/settings/space", client.mode === "fixture")}>{tr("打开个人空间设置")} <ArrowRight size={14} /></Link></p></div>}
      <div className="aiw-share-list">{state.services.map(service => <section className="aiw-card aiw-share-card" key={service.kind}><div className={`aiw-provider-icon ${service.kind}`}><Terminal size={26} /></div><div className="aiw-provider-main"><div className="aiw-provider-title"><h2>{service.title}</h2><Badge tone={service.publication_enabled && service.online ? "success" : service.installed ? "neutral" : "warning"}>{service.publication_enabled && service.online ? tr("正在分享") : service.installed ? service.configured ? tr("已配置") : tr("待配置") : tr("未安装")}</Badge></div><p>{service.kind === "codex_cli" ? tr("调用家里电脑上已登录的 Codex CLI") : tr("调用家里电脑上已登录的 Claude Code")}</p><div className="aiw-provider-tags"><span><Cloud size={14} /> {tr("CLI 使用其登录账号的云端模型")}</span><span><ShieldCheck size={14} /> {tr("API 仅文本推理")}</span></div></div><div className="aiw-provider-actions">{!service.configured ? <button className="aiw-primary" disabled={!service.installed || Boolean(busy)} onClick={() => void setup(service)}>{busy === service.kind ? tr("正在验证…") : tr("配置并验证")}</button> : <button className={service.publication_enabled ? "aiw-button" : "aiw-primary"} disabled={Boolean(busy) || !service.online || (!service.publication_enabled && !state.personal_space_ready)} onClick={() => void share(service)}>{busy === service.kind ? tr("正在更新…") : service.publication_enabled ? tr("停止分享") : tr("分享给我的节点")}</button>}{!service.installed && <small>{tr("先在这台电脑安装并登录 CLI")}</small>}</div></section>)}</div>
      <section className="aiw-card"><div className="aiw-section-heading"><div><span className="aiw-eyebrow">{tr("OTHER SERVICES")}</span><h2>{tr("本地模型")}</h2></div><Link to={personalHref("/services/manage", client.mode === "fixture")} className="aiw-text-link">{tr("管理本地模型")} <ArrowRight size={15} /></Link></div><p className="aiw-card-description">{tr("Qwen 等本地模型继续按现有方式分享推理能力。CLI Agent 则使用家里电脑的登录环境调用云端模型，两者会在笔记本的 API 模型列表中分别显示。")}</p></section>
      <div className="aiw-note"><CircleHelp size={18} /><p>{tr("当前 CLI 服务仅支持文本对话，不向调用者提供文件或命令工具。CLI 进程仍以这台电脑的登录用户运行，请仅向可信任的个人节点分享。")}</p></div>
    </>}
  </div>;
}
