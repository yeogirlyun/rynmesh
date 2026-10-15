import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { AgentAPIError, canUseAgentAction, canUseAgentModel, rynAgent, type AgentModel, type AgentModelSwitchPreview, type AgentModelSwitchRequest, type AgentTask } from "../domain/rynAgent";

export default function RynAgentModel({ task, models, defaultModelID, busy, onChange, expanded = false, initialModelID = "" }: { task: AgentTask; models: AgentModel[]; defaultModelID?: string; busy: boolean; onChange: (task: AgentTask) => void; expanded?: boolean; initialModelID?: string }) {
  const { i18n } = useTranslation();
  const text = (zh: string, en: string) => i18n.language.startsWith("zh") ? zh : en;
  const [modelID, setModelID] = useState(initialModelID);
  const [preview, setPreview] = useState<AgentModelSwitchPreview | null>(null);
  const [open, setOpen] = useState(expanded);
  const [consent, setConsent] = useState(false);
  const [transfer, setTransfer] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const storageKey = `ryn.agent.pending-model/${task.id}`;
  const [pending, setPending] = useState<AgentModelSwitchRequest | null>(() => { try { return JSON.parse(sessionStorage.getItem(storageKey) || "null"); } catch { return null; } });
  const lock = useRef(false);
  const allowed = canUseAgentAction(task, "model");
  const currentName = models.find(model => model.id === task.model_id)?.name || task.model_id;
  const availabilityLabel = (model: AgentModel) => !model.available ? text("不可用", "Unavailable")
    : model.source === "peer" && model.destination_binding_supported === false ? text("接入信息暂不可核对", "Destination unavailable")
    : !canUseAgentModel(model) ? text("不支持助手工具", "Assistant tools unsupported") : "";
  const remember = (value: AgentModelSwitchRequest | null) => {
    setPending(value);
    try { if (value) sessionStorage.setItem(storageKey, JSON.stringify(value)); else sessionStorage.removeItem(storageKey); } catch { /* The exact request remains in component memory. */ }
  };
  useEffect(() => {
    if (!open || !allowed || !modelID || pending) return;
    const abort = new AbortController(); setPreview(null); setConsent(false); setTransfer(false); setLoading(true); setError("");
    void rynAgent.previewModel(task.id, modelID, abort.signal).then(value => { if (!abort.signal.aborted) setPreview(value); })
      .catch(reason => { if (!abort.signal.aborted) setError(reason instanceof AgentAPIError && reason.code === "agent_model_destination_unverifiable" ? text("无法核对该模型的资料接收方，暂不能切换到此模型。", "This model's data destination cannot be verified. Switching is unavailable.") : text("无法读取切换预览，请核对任务安全检查点和模型状态。", "Could not read the switch preview. Review the task checkpoint and model state.")); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [open, allowed, modelID, task.id, task.control_revision, task.scope_revision, pending, reload]);
  const save = async () => {
    if (busy || lock.current || (!pending && (!allowed || !consent || !preview))) return;
    const body = pending || { model_id: preview!.model.id, expected_control_revision: preview!.control_revision, expected_scope_revision: preview!.scope_revision, expected_destination: preview!.destination, confirm_data_destination: true as const, transfer_resource_scope: transfer, client_request_id: crypto.randomUUID() };
    remember(body); lock.current = true; setSaving(true); setError("");
    try { const next = await rynAgent.switchModel(task.id, body); remember(null); setPreview(null); setModelID(""); setConsent(false); setTransfer(false); onChange(next); }
    catch (reason) {
      if (reason instanceof AgentAPIError && reason.status < 500) { remember(null); setPreview(null); setConsent(false); setTransfer(false); }
      setError(reason instanceof AgentAPIError && reason.status < 500 ? text("任务、目录权限或模型接收方已变化，请重新读取预览并确认。", "The task, directory permissions or model destination changed. Read a fresh preview and confirm it.") : text("切换结果未确认。重试只会核对原始请求，不会改变目的地或自动继续。", "The switch is unconfirmed. Retry checks the original request without changing its destination or resuming."));
    } finally { lock.current = false; setSaving(false); }
  };
  if (!task.allowed_actions?.model && !pending) return null;
  return <details className="ra-task-context ra-model-switch" open={expanded || undefined} onToggle={event => setOpen(event.currentTarget.open)}><summary>{text("任务模型", "Task model")} · {currentName}</summary>{open && <>
    <p>{text("只更改当前任务的模型，助手默认模型保持独立。保存后任务暂停，不会自动继续。", "Changes only this task's model. The assistant default stays independent. Saving pauses the task without resuming it.")}</p>
    {defaultModelID && <p>{text("新任务默认模型", "Default for new tasks")}: {models.find(model => model.id === defaultModelID)?.name || defaultModelID}</p>}
    {!allowed && !pending && <p>{text("请先暂停到安全检查点。运行中的请求或结果未知的写入未结束时不能切换。", "Pause at a safe checkpoint first. Switching is unavailable during a request or an unresolved write.")}</p>}
    <label>{text("新任务模型接收方", "New model destination for this task")}<select aria-label={text("切换任务模型", "Switch task model")} value={modelID} disabled={!allowed || busy || saving || Boolean(pending)} onChange={event => setModelID(event.target.value)}><option value="">{text("请选择模型", "Choose a model")}</option>{models.map(model => <option key={model.id} value={model.id} disabled={!canUseAgentModel(model)}>{model.name} · {model.source_name || model.source}{model.id === task.model_id ? text(" · 当前模型，可重新确认接收方", " · Current model; reconfirm destination") : ""}{availabilityLabel(model) ? ` · ${availabilityLabel(model)}` : ""}</option>)}</select></label>
    {loading && <p role="status">{text("正在核对接收方…", "Checking the destination…")}</p>}
    {error && <p role="alert">{error} {!pending && <button disabled={busy || saving || loading} onClick={() => setReload(value => value + 1)}>{text("重新读取预览", "Read preview again")}</button>}</p>}
    {preview && <>
      <p>{text("资料接收方", "Data destination")}: <strong>{preview.model.name}</strong> · {preview.model.source_name || preview.model.source}<br /><small>{text("来源", "Source")}: {preview.destination.source_id || text("未知", "Unknown")} · {text("提供节点", "Provider node")}: {preview.destination.provider_peer_id || text("未知", "Unknown")}</small></p>
      <p>{text("显式继续后，历史用户消息、当前目标、已选技能与当前偏好会发给新模型。原模型回复、工具正文和摘要不会转发；目标、累计预算和审计记录仍保留。旧审批和等待回答会失效。", "After an explicit resume, previous owner messages, the current goal, selected skills and current preferences go to the new model. Previous model replies, tool contents and summaries are excluded. The goal, cumulative budget and audit records remain. Pending approvals and questions expire.")}</p>
      {(preview.current_scope_selection.resources.length > 0 || preview.current_project) && <>
        {preview.current_project && <p>{text("项目", "Project")}: <strong>{preview.current_project.name}</strong> · <code>{preview.current_project.path}</code></p>}
        <ul>{preview.current_scope_selection.resources.map((resource, index) => <li key={index}><code>{resource.connection_id} · {resource.root_path || "/"}</code> · {resource.actions.includes("create_file") || resource.actions.includes("create_directory") ? text("含新建权限", "Includes creation permission") : text("只读", "Read only")}</li>)}</ul>
        <label className="ra-scope-check"><input type="checkbox" checked={transfer} disabled={busy || saving || Boolean(pending) || preview.current_scope_authorized !== true} onChange={event => { setTransfer(event.target.checked); setConsent(false); }} />{text("重新授权上述项目和目录给这个新模型，允许内容发送到此接收方。", "Reauthorize this project and these directories for the new model and allow their contents to be sent to this destination.")}</label>
        <p>{text("默认不转授权：项目和目录访问会撤销。目录选择保留；项目可通过新建任务重新加载。", "By default, project and directory access are revoked. Directory selections remain; load a project again through a new task.")}</p>
      </>}
      <label className="ra-scope-check"><input type="checkbox" checked={consent} disabled={busy || saving || Boolean(pending)} onChange={event => setConsent(event.target.checked)} />{text("我确认上述用户文本与已选择的资料可发给此模型接收方。", "I confirm that the described owner text and selected data may be sent to this model destination.")}</label>
    </>}
    {pending && <p role="status">{text("原始切换请求待核对，不会自动重发。", "The original switch request needs checking. It is not replayed automatically.")}</p>}
    {(preview || pending) && <button className="ra-primary" disabled={busy || saving || (!pending && (!consent || !allowed))} onClick={() => void save()}>{pending ? text("核对原始切换请求", "Check original switch request") : text("保存任务模型，保持暂停", "Save task model and keep paused")}</button>}
  </>}</details>;
}
