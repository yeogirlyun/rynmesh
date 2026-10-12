import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { AgentAPIError, canUseAgentAction, rynAgent, type AgentTask } from "../domain/rynAgent";
import ChatMarkdown from "./ChatMarkdown";

/** Secrets stay in this component's memory and go only to the owner vault API. */
export default function RynAgentCredential({ task, busy, onSaved }: { task: AgentTask; busy: boolean; onSaved: (task: AgentTask) => void }) {
  const { i18n } = useTranslation();
  const zh = i18n.language.startsWith("zh");
  const text = (cn: string, en: string) => zh ? cn : en;
  const [kind, setKind] = useState("api");
  const [endpoint, setEndpoint] = useState("");
  const [sourceID, setSourceID] = useState("");
  const [secret, setSecret] = useState("");
  const [reuse, setReuse] = useState(false);
  const [consent, setConsent] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [uncertain, setUncertain] = useState(false);
  const request = useRef<Record<string, unknown> | null>(null);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; request.current = null; }; }, []);
  const enabled = canUseAgentAction(task, "source_credential");
  const valid = consent && endpoint.trim() && (reuse ? sourceID.trim() : secret.trim()) && enabled;
  const locked = busy || saving || uncertain;
  const send = async () => {
    if (busy || saving || (!uncertain && !valid)) return;
    if (!request.current) request.current = {
      client_request_id: crypto.randomUUID(), kind, base_url: endpoint.trim(), source_id: sourceID.trim(),
      ...(reuse ? { reuse_existing: true } : { api_key: secret }),
      expected_control_revision: task.control_revision, expected_goal_revision: task.goal?.revision,
      pending_input_id: task.pending_input?.id,
    };
    setSaving(true); setError("");
    try {
      const result = await rynAgent.sourceCredential(task.id, request.current);
      request.current = null;
      if (!alive.current) return;
      setSecret(""); setUncertain(false); setConsent(false);
      onSaved(result.task);
    } catch (reason) {
      if (!alive.current) return;
      if (reason instanceof AgentAPIError && reason.status >= 400 && reason.status < 500) {
        request.current = null; setSecret(""); setUncertain(false); setConsent(false);
        setError(text("请求未完成确认。任务或来源可能已变化，请刷新任务后重新确认；凭据可能已存入本机，但任务尚未接收。", "The request was not confirmed. Refresh and review the task or source. The credential may be stored locally without the task receiving it."));
      } else {
        setUncertain(true);
        setError(text("保存结果尚未确认。可以重试同一请求；请勿重新创建凭据。离开此页面会清除本次输入。", "The save result is unconfirmed. Retry the same request. Leaving this page clears this input."));
      }
    } finally { if (alive.current) setSaving(false); }
  };
  return <section className="ra-input-request ra-approval ra-credential" aria-label={text("安全凭据表单", "Secure credential form")}>

    <details className="ra-credential-explanation"><summary>{text("查看助手的接入要求", "View the assistant’s connection request")}</summary>{task.pending_input?.prompt && <ChatMarkdown>{task.pending_input.prompt}</ChatMarkdown>}</details>
    <p>{text("密钥只提交到本机安全存储，不进入聊天、模型提示或浏览器草稿。保存凭据不会保存来源配置或开启分享。", "The key goes only to local secure storage, never chat, model prompts or browser drafts. Saving a credential does not save a source or enable sharing.")}</p>
    <div className="ra-answer-fields">
      <label><span>{text("来源类型", "Source type")}</span><select value={kind} disabled={locked} onChange={event => { setKind(event.target.value); setConsent(false); }}><option value="api">{text("云端 HTTP API", "Cloud HTTP API")}</option><option value="local">{text("本机 HTTP 服务", "Local HTTP service")}</option></select></label>
      <label><span>{text("确认服务地址", "Confirm endpoint")}</span><input value={endpoint} type="url" autoComplete="off" placeholder={kind === "api" ? "https://api.example.com/v1" : "http://127.0.0.1:8000/v1"} disabled={locked} onChange={event => { setEndpoint(event.target.value); setConsent(false); }} /></label>
      <label><span>{text("已有来源 ID（新增来源留空）", "Existing source ID (empty for a new source)")}</span><input value={sourceID} autoComplete="off" disabled={locked} onChange={event => { setSourceID(event.target.value); setConsent(false); }} /></label>
      <label><input type="checkbox" checked={reuse} disabled={locked} onChange={event => { setReuse(event.target.checked); setSecret(""); setConsent(false); }} />{text("使用该来源现有密钥（地址必须一致）", "Reuse this source's existing key (same endpoint required)")}</label>
      {!reuse && <label><span>API Key</span><input type="password" value={secret} autoComplete="new-password" spellCheck={false} disabled={locked} onChange={event => setSecret(event.target.value)} /></label>}
      <label><input type="checkbox" checked={consent} disabled={locked} onChange={event => setConsent(event.target.checked)} />{text("我确认该地址可以接收此凭据，用于本任务的来源测试。", "I confirm this endpoint may receive this credential for this task's source tests.")}</label>
    </div>
    {error && <p role="alert">{error}</p>}
    {!enabled && <p>{text("当前任务状态不允许配置凭据，请刷新后检查。", "Credential changes are unavailable in the current task state. Refresh and review it.")}</p>}
    <button className="ra-primary" disabled={busy || saving || (!uncertain && !valid)} onClick={() => void send()}>{saving ? text("正在保存…", "Saving…") : uncertain ? text("重试同一保存请求", "Retry the same save") : text("安全保存并继续", "Save securely and continue")}</button>
  </section>;
}
