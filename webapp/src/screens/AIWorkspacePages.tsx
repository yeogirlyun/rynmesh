import { tr, useUILanguage, uiLocale } from "../uiI18n";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, ArrowRight, Check, Copy, Info, KeyRound, LockKeyhole, Plus, RefreshCw, ShieldCheck } from "lucide-react";
import { useAppContext } from "../appContext";
import type { CLIServiceStatus, CLIServicesStatus, InferenceAccess } from "../domain/nodeClient";
import { personalHref } from "../personal/model";
import { LaptopGlyph, DesktopGlyph, GlyphTile, ShieldGlyph, TermGlyph } from "./ServiceParts";
import "./service-pages-refresh.css";

type Target = InferenceAccess["targets"][number];
type Tone = "ok" | "off" | "warn" | "self";
const aliasPattern = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$/;

function Pill({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  useUILanguage();
  return <span className={`rf-pill rf-pill-sm rf-t-${tone}`}><i aria-hidden="true" />{children}</span>;
}

function Loading() {
  useUILanguage(); return <div className="sv-state"><RefreshCw size={18} className="sv-spin" aria-hidden="true" />{tr("正在读取此节点的配置…")}</div>; }
function Failure({ message, retry }: { message: string; retry: () => void }) {
  useUILanguage(); return <div className="sv-state sv-state-bad" role="alert"><span>{message}</span><button className="rf-btn rf-btn-sm" onClick={retry}>{tr("重试")}</button></div>; }

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
  if (target.rynmesh.adapter === "antigravity_cli") return "Antigravity";
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
  const mappingHref = personalHref("/services/model-mapping", client.mode === "fixture");
  const activeKeys = access ? access.keys.filter(key => !key.revoked).length : 0;
  return <div className="rf rd-svc rd-api">
    <header className="sv-head">
      <div className="sv-head-text"><h1 className="sv-title">{tr("API 接入")}</h1><p className="sv-lead">{tr("让笔记本上的其他程序，通过本机节点调用家里电脑的 AI 服务。")}</p></div>
      <div className="sv-head-actions"><Link to={mappingHref} className="rf-btn">{tr("模型映射")}</Link></div>
    </header>
    <section className="sv-card sv-card-flat sv-path" aria-label={tr("Request path")}>
      <div className="sv-path-end rf-h-indigo"><GlyphTile hue="indigo" size="sm">{px => <LaptopGlyph size={px} />}</GlyphTile><div><small>{tr("Current device")}</small><b>{node.node_name}</b></div></div>
      <div className="sv-path-mid"><span className="rf-pill rf-t-self rf-pill-line"><ShieldCheck size={12} aria-hidden="true" />{tr("Ryn 加密连接")}</span><span className="sv-path-line" aria-hidden="true" /></div>
      <div className="sv-path-end sv-right rf-h-teal"><div><small>{tr("家里节点执行")}</small><b>{model ? (model.rynmesh.source === "peer" ? tr("远端节点") : tr("本机节点")) : "—"}</b></div><GlyphTile hue="teal" size="sm">{px => <DesktopGlyph size={px} />}</GlyphTile></div>
    </section>
    {loading && !access ? <Loading /> : error && !access ? <Failure message={error} retry={() => void refresh()} /> : access && <>
      <section className="sv-card sv-card-pad" aria-labelledby="sv-ep-title">
        <div className="sv-sec-head"><div><h2 id="sv-ep-title" className="sv-h2 sv-h2-lg">{tr("本机 API 地址")}</h2><p className="sv-sub">{tr("其他程序只需连接笔记本上的节点地址，节点会负责路由和格式转换。")}</p></div><Pill tone="ok">{tr("仅本机监听")}</Pill></div>
        <div className="rf-seg" role="group" aria-label={tr("API format")}><button type="button" aria-pressed={protocol === "openai"} onClick={() => setProtocol("openai")}>{tr("OpenAI 兼容")}</button><button type="button" aria-pressed={protocol === "anthropic"} onClick={() => setProtocol("anthropic")}>{tr("Anthropic 兼容")}</button></div>
        <div className="sv-url"><code>{base}</code><button type="button" className="rf-btn rf-btn-sm" aria-label={tr("复制 API 地址")} onClick={() => void copyText(base, copied)}><Copy size={13} aria-hidden="true" />{tr("复制地址")}</button></div>
        <p className="rf-note"><LockKeyhole size={14} aria-hidden="true" /><span>{tr("API 不提供文件或命令参数；CLI 仍以家里电脑的登录用户运行，请只分享给可信任的个人节点。")}</span></p>
      </section>
      <div className="sv-two">
        <section className="sv-card sv-card-pad" aria-labelledby="sv-models-title"><div className="sv-sec-head"><div><h2 id="sv-models-title" className="sv-h2">{tr("调用模型")}</h2></div><Link to={mappingHref} className="rf-lnk">{tr("管理映射")} <ArrowRight size={14} aria-hidden="true" /></Link></div>
          <label className="sv-field"><span>{tr("模型名称")}</span><select className="sv-inp sv-mono" value={model?.id || ""} onChange={event => setSelectedModel(event.target.value)}>{access.models.map(item => <option value={item.id} key={item.id}>{item.id}</option>)}</select></label>
          {model ? <div className="sv-model-box"><span>{targetKind(model)} · {model.rynmesh.source === "peer" ? tr("远端节点") : tr("本机节点")}</span><Pill tone="ok">{tr("可调用")}</Pill></div> : <p className="sv-empty sv-empty-plain">{tr("暂无可调用模型。请先连接并发布家里的服务。")}</p>}
        </section>
        <section className="sv-card sv-card-pad" aria-labelledby="sv-key-title"><div className="sv-sec-head"><div><h2 id="sv-key-title" className="sv-h2">{tr("创建项目密钥")}</h2></div></div>
          <div className="sv-grid2"><label className="sv-field"><span>{tr("项目名称")}</span><input className="sv-inp" value={project} onChange={event => setProject(event.target.value)} maxLength={80} /></label><label className="sv-field"><span>{tr("输出 Token 额度")}</span><input className="sv-inp" type="number" min={1} max={1000000000} value={limit} onChange={event => setLimit(Number(event.target.value))} /></label></div>
          <button type="button" className="rf-btn rf-btn-p" style={{ alignSelf: "flex-start" }} disabled={busy || !project.trim() || limit < 1} onClick={() => void createKey()}><Plus size={14} aria-hidden="true" />{tr("创建密钥")}</button>
          {freshKey && <div className="sv-once"><b>{tr("仅显示这一次，请妥善保存")}</b><div className="sv-once-row"><code>{freshKey}</code><button type="button" className="rf-btn rf-btn-sm" onClick={() => void copyText(freshKey, copied)}><Copy size={13} aria-hidden="true" />{tr("复制")}</button></div><button type="button" className="rf-lnk rf-lnk-sm" style={{ alignSelf: "flex-start" }} onClick={() => setFreshKey("")}>{tr("我已保存，隐藏密钥")}</button></div>}
        </section>
      </div>
      <section className="sv-card" aria-labelledby="sv-keys-title"><div className="sv-bar"><h2 id="sv-keys-title" className="sv-h2">{tr("已有项目密钥")}</h2><span className="sv-count">{activeKeys} {tr("个有效")}</span></div>
        {access.keys.length ? <div className="sv-keys">{access.keys.map(key => {
          const pct = key.output_token_limit > 0 ? Math.min(100, Math.round(key.used_output_tokens / key.output_token_limit * 100)) : 0;
          const tone: Tone = key.revoked ? "off" : pct > 80 ? "warn" : "ok";
          return <div className="sv-key" key={key.id} data-off={Boolean(key.revoked)}>
            <span className="rf-chipicon rf-h-amber" aria-hidden="true"><KeyRound size={16} /></span>
            <div className="sv-key-text"><b>{key.name}</b><small>{tr("已使用")} {key.used_output_tokens.toLocaleString(uiLocale())} / {key.output_token_limit.toLocaleString(uiLocale())} {tr("输出 Token")}</small></div>
            <span className="sv-key-bar" aria-hidden="true" data-high={pct > 80}><i style={{ width: `${pct}%` }} /></span>
            <Pill tone={tone}>{key.revoked ? tr("已撤销") : tr("有效")}</Pill>
            {!key.revoked && <button type="button" className="rf-btn rf-btn-sm" aria-label={`${tr("撤销密钥")} ${key.name}`} disabled={busy} onClick={() => void revokeKey(key.id)}>{tr("撤销密钥")}</button>}
          </div>;
        })}</div> : <p className="sv-empty sv-empty-plain" style={{ margin: 16 }}>{tr("还没有项目密钥。创建后即可让本机程序调用模型。")}</p>}
      </section>
      <section className="sv-card sv-card-pad" aria-labelledby="sv-code-title"><div className="sv-sec-head"><div><h2 id="sv-code-title" className="sv-h2">{tr("代码示例")} <span className="sv-h2-sub">· {protocol === "openai" ? tr("OpenAI 兼容") : tr("Anthropic 兼容")}</span></h2></div><button type="button" className="rf-btn rf-btn-sm" onClick={() => void copyText(sample, copied)}>{tr("复制代码")}</button></div><pre className="sv-pre"><code>{sample}</code></pre><p className="sv-footnote">{tr("将 YOUR_RYNMESH_KEY 换成上方创建的密钥。远端模型请求使用节点间加密连接。")}</p></section>
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
  const aliases = access ? Object.entries(access.aliases) : [];
  return <div className="rf rd-svc rd-mapping">
    <header className="sv-head">
      <div className="sv-head-text"><h1 className="sv-title">{tr("模型映射")}</h1><p className="sv-lead">{tr("给来自不同节点的服务一个稳定名称，让笔记本上的程序无需关心它在哪台电脑运行。")}</p></div>
      <div className="sv-head-actions"><Link to={personalHref("/services/api", client.mode === "fixture")} className="rf-btn"><ArrowLeft size={14} aria-hidden="true" />{tr("API 接入")}</Link></div>
    </header>
    {loading && !access ? <Loading /> : error && !access ? <Failure message={error} retry={() => void refresh()} /> : access && <div className="sv-two" style={{ alignItems: "flex-start" }}>
      <section className="sv-card" aria-labelledby="sv-saved-title"><div className="sv-bar"><h2 id="sv-saved-title" className="sv-h2">{tr("已保存的映射")}</h2><span className="sv-count">{aliases.length} {tr("条")}</span></div>
        {aliases.length ? <div>{aliases.map(([alias, id]) => { const target = access.targets.find(item => item.id === id); return <button type="button" className="sv-route" key={alias} aria-pressed={alias === name} onClick={() => { setName(alias); setTargetId(id); }}><code>{alias}</code><ArrowRight size={16} aria-hidden="true" /><span className="sv-route-text"><b>{target ? labelFor(target) : tr("当前不可用")}</b><small>{target ? targetKind(target) : id}</small></span><Pill tone={target ? "ok" : "warn"}>{target ? tr("可用") : tr("离线")}</Pill></button>; })}</div> : <p className="sv-empty sv-empty-plain" style={{ margin: 16 }}>{tr("还没有映射。下方选择一个服务并创建别名。")}</p>}
        <div className="sv-info"><Info size={14} aria-hidden="true" /><p>{tr("映射保存在当前笔记本节点。API 调用中的")} <code className="sv-code">{tr("model")}</code> {tr("使用左侧名称，节点会将请求转到右侧服务，并输出统一的 API 格式。")}</p></div>
      </section>
      <section className="sv-card sv-card-pad" aria-labelledby="sv-edit-title"><h2 id="sv-edit-title" className="sv-h2">{tr("创建或修改映射")}</h2>
        <div className="sv-map-grid"><label className="sv-field"><span>{tr("对外模型名称")}</span><input className="sv-inp sv-mono" placeholder={tr("例如：home-codex")} value={name} onChange={event => setName(event.target.value)} maxLength={64} /></label><ArrowRight size={18} aria-hidden="true" /><label className="sv-field"><span>{tr("实际服务")}</span><select className="sv-inp" value={selected?.id || ""} onChange={event => setTargetId(event.target.value)}>{access.targets.map(item => <option key={item.id} value={item.id}>{labelFor(item)} · {targetKind(item)}</option>)}</select></label></div>
        {selected ? <div className="sv-detail"><b>{selected.rynmesh.model_alias}</b><small>{targetKind(selected)} · {selected.rynmesh.source === "peer" ? tr("家里 / 远端节点") : tr("当前节点")}</small><small>{tr("最大输出")} {selected.rynmesh.max_output_tokens.toLocaleString(uiLocale())} Token</small></div> : <p className="sv-empty sv-empty-plain">{tr("暂无可用服务。先连接家里的节点或配置本机模型。")}</p>}
        <div className="sv-actions"><button type="button" className="rf-btn rf-btn-p rf-btn-lg" disabled={saving || !selected || !aliasPattern.test(name)} onClick={() => void save()}><Check size={14} aria-hidden="true" />{tr("保存映射")}</button><small>{tr("可使用字母、数字、点、短横线和下划线，最长 64 个字符。")}</small></div>
      </section>
    </div>}
  </div>;
}

const CLI_HUE: Record<string, "slate" | "amber" | "sky"> = { codex_cli: "slate", claude_cli: "amber", antigravity_cli: "sky" };

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
  const fixture = client.mode === "fixture";
  return <div className="rf rd-svc rd-sharing">
    <header className="sv-head"><div className="sv-head-text"><h1 className="sv-title">{tr("服务共享")}</h1><p className="sv-lead">{tr("把家里电脑上的 CLI Agent 分享给自己的笔记本节点，再由笔记本提供本机 API。")}</p></div></header>
    <section className="sv-card sv-card-flat sv-flow" aria-label={tr("How sharing works")}>
      <div className="sv-flow-step rf-h-teal"><GlyphTile hue="teal" size="xs">{px => <TermGlyph size={px} />}</GlyphTile><b>{tr("家里电脑上的 CLI")}</b></div>
      <ArrowRight size={18} aria-hidden="true" />
      <div className="sv-flow-step sv-mid rf-h-indigo"><GlyphTile hue="indigo" size="xs">{px => <ShieldGlyph size={px} />}</GlyphTile><b>{tr("个人空间节点")}</b></div>
      <ArrowRight size={18} aria-hidden="true" />
      <div className="sv-flow-step sv-last rf-h-sky"><b>{tr("笔记本对话 + API")}</b><GlyphTile hue="sky" size="xs">{px => <LaptopGlyph size={px} />}</GlyphTile></div>
    </section>
    {loading && !state ? <Loading /> : error && !state ? <Failure message={error} retry={() => void refresh()} /> : state && <>
      {state.services.some(service => service.configured && service.online) && <div className="sv-alert sv-alert-ok"><Check size={17} aria-hidden="true" /><p>{tr("本机 CLI 已就绪，当前电脑可以直接使用。")}</p><Link className="rf-lnk" to={personalHref("/services/private-ai/chat", fixture)}>{tr("打开 AI 工作台")} <ArrowRight size={14} aria-hidden="true" /></Link></div>}
      {!state.personal_space_ready && <div className="sv-alert"><LockKeyhole size={17} aria-hidden="true" /><p>{tr("分享 CLI 服务前，请先加入个人空间，并在家里节点开启“个人空间 AI 访问”。")}</p><Link className="rf-lnk" to={personalHref("/settings/space", fixture)}>{tr("打开个人空间设置")} <ArrowRight size={14} aria-hidden="true" /></Link></div>}
      <div className="sv-clis">{state.services.map(service => {
        const sharing = service.publication_enabled && service.online;
        const tone: Tone = sharing ? "ok" : service.installed ? service.configured ? "self" : "off" : "warn";
        return <section className={`sv-card sv-cli rf-h-${CLI_HUE[service.kind] ?? "slate"}`} key={service.kind}>
          <div className="sv-cli-top"><GlyphTile hue={CLI_HUE[service.kind] ?? "slate"} size="lg" off={!service.installed}>{px => <TermGlyph size={px} />}</GlyphTile><div><h2>{service.title}</h2><small>{service.kind === "codex_cli" ? tr("调用家里电脑上已登录的 Codex CLI") : service.kind === "antigravity_cli" ? tr("调用家里电脑上已登录的 Antigravity") : tr("调用家里电脑上已登录的 Claude Code")}</small></div></div>
          <span className="sv-cli-pill"><Pill tone={tone}>{sharing ? tr("正在分享") : service.installed ? service.configured ? tr("已配置") : tr("待配置") : tr("未安装")}</Pill></span>
          <ul className="sv-bullets"><li>· {tr("CLI 使用其登录账号的云端模型")}</li><li>· {tr("API 仅文本推理")}</li></ul>
          <div className="sv-cli-foot">{!service.configured ? <button type="button" className="rf-btn rf-btn-p" disabled={!service.installed || Boolean(busy)} onClick={() => void setup(service)}>{busy === service.kind ? tr("正在接入…") : tr("配置接入")}</button> : <button type="button" className={`rf-btn${service.publication_enabled ? "" : " rf-btn-p"}`} disabled={Boolean(busy) || !service.online || (!service.publication_enabled && !state.personal_space_ready)} onClick={() => void share(service)}>{busy === service.kind ? tr("正在更新…") : service.publication_enabled ? tr("停止分享") : tr("分享给我的节点")}</button>}{!service.installed && <small>{tr("先在这台电脑安装并登录 CLI")}</small>}</div>
        </section>;
      })}</div>
      <section className="sv-card sv-local" aria-labelledby="sv-local-title"><div><h2 id="sv-local-title" className="sv-h2">{tr("本地模型")}</h2><p>{tr("Qwen 等本地模型继续按现有方式分享推理能力。CLI Agent 则使用家里电脑的登录环境调用云端模型，两者会在笔记本的 API 模型列表中分别显示。")}</p></div><Link to={personalHref("/services/manage", fixture)} className="rf-btn">{tr("管理本地模型")}</Link></section>
      <p className="rf-note"><Info size={14} aria-hidden="true" /><span>{tr("当前 CLI 服务仅支持文本对话，不向调用者提供文件或命令工具。CLI 进程仍以这台电脑的登录用户运行，请仅向可信任的个人节点分享。")}</span></p>
    </>}
  </div>;
}
