import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { applyMcpServerHints, type ExtensionDataPolicy, type ExtensionMcpServer, type ExtensionServerHintsInput } from "../domain/agentExtensions";

export function McpDataPolicySummary({ policy, compact = false }: { policy?: ExtensionDataPolicy; compact?: boolean }) {
  const { i18n } = useTranslation();
  const tx = (zh: string, en: string) => i18n.language.startsWith("zh") ? zh : en;
  const current = policy || { models: { mode: "any" }, memory: "allow" };
  const restricted = current.models?.mode === "allowlist";
  return <section className={compact ? undefined : "ae-section-card"} aria-label={tx("数据策略", "Data policy")}>
    {compact ? <strong>{tx("数据策略", "Data policy")}</strong> : <h3>{tx("数据策略", "Data policy")}</h3>}
    <p>{restricted ? tx("模型：仅允许以下白名单", "Models: only the following allowlist") : tx("模型：不限制", "Models: unrestricted")}</p>
    {current.models?.mode === "allowlist" ? current.models.allow.length ? <ul>{current.models.allow.map((rule, index) => <li key={index}>
      {"on_device" in rule ? tx("本机内置推理模型", "Built-in inference on this device")
        : "api_base_url_prefix" in rule ? <>{tx("API 地址范围：", "API address prefix: ")}<code>{rule.api_base_url_prefix}</code></>
          : <>{tx("模型 ID：", "Model ID: ")}<code>{rule.catalog_model_id}</code></>}
    </li>)}</ul> : <p>{tx("白名单为空，任何模型均不可使用这些工具。", "The allowlist is empty; no model can use these tools.")}</p> : null}
    <p>{current.memory === "deny" ? tx("长期记忆：禁止。使用这些工具的任务不会生成自动长期记忆。", "Long-term memory: denied. Tasks using these tools do not create automatic long-term memories.")
      : restricted ? tx("长期记忆：策略允许；当前版本对模型受限任务仍禁用自动长期记忆，避免其他模型读取。", "Long-term memory: allowed by the policy; this version still disables automatic long-term memory for model-restricted tasks so other models cannot read it.")
        : tx("长期记忆：允许，仍遵循任务的其他设置。", "Long-term memory: allowed, subject to the task's other settings.")}</p>
    {!compact ? <p>{tx("策略来自导入包，在此只读；更改需重新导入。它约束正常的 Agent 运行，不防止本人修改本机源码或数据文件。", "This policy comes from the imported package and is read-only here; changes require reimporting. It governs normal Agent operation and does not prevent the owner from editing local source or data files.")}</p> : null}
  </section>;
}

export function McpDataPolicy({ server, onRefresh }: { server: ExtensionMcpServer; onRefresh: () => void }) {
  const { i18n } = useTranslation();
  const tx = (zh: string, en: string) => i18n.language.startsWith("zh") ? zh : en;
  const candidates = server.server_hint_candidates || [];
  const anchor = JSON.stringify([server.id, server.instance_id, server.revision, server.discovery_revision, candidates]);
  const currentAnchor = useRef(anchor); currentAnchor.current = anchor;
  const [selected, setSelected] = useState<string[]>([]);
  const [review, setReview] = useState<{ anchor: string; payload: ExtensionServerHintsInput } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    currentAnchor.current = anchor;
    setSelected([]); setReview(null); setBusy(false); setError(false); setSaved(false);
    return () => { currentAnchor.current = ""; };
  }, [anchor]);
  const canReview = Boolean(server.instance_id && Number.isInteger(server.discovery_revision) && (server.discovery_revision || 0) > 0);
  async function apply() {
    if (!review || review.anchor !== anchor || busy) return;
    const pending = review;
    setBusy(true); setError(false);
    try {
      await applyMcpServerHints(server.id, pending.payload);
      if (currentAnchor.current !== pending.anchor) return;
      setReview(null); setSelected([]); setSaved(true); onRefresh();
    } catch {
      if (currentAnchor.current === pending.anchor) { setError(true); setReview(null); setSelected([]); }
    } finally { if (currentAnchor.current === pending.anchor) setBusy(false); }
  }
  return <>
    <McpDataPolicySummary policy={server.data_policy} />
    {candidates.length ? <section className="ae-section-card" aria-label={tx("服务器只读提示", "Server read-only hints")}>
      <h3>{tx("按服务器提示设为只读", "Use server read-only hints")}</h3>
      <p>{tx("服务器将以下工具标为只读。仅在你确认后，选中的工具才会免去每次调用确认。", "The server marks these tools as read-only. Selected tools skip per-call confirmation only after you explicitly confirm.")}</p>
      <fieldset className="ae-subcard-fieldset" disabled={busy || !canReview}><legend>{tx("选择工具（默认不选）", "Choose tools (none selected by default)")}</legend>
        <div className="ae-inline-actions">
          <button type="button" className="ae-btn ae-btn-sm" onClick={() => { setReview(null); setError(false); setSaved(false); setSelected(candidates.map(candidate => candidate.name)); }}>{tx("选择全部", "Select all")}</button>
          <button type="button" className="ae-btn ae-btn-sm" onClick={() => { setReview(null); setError(false); setSaved(false); setSelected([]); }}>{tx("清空选择", "Clear selection")}</button>
        </div>
        <div className="ae-checkbox-list">
          {candidates.map(candidate => <label key={candidate.name} className="ae-checkbox-item"><input type="checkbox"
            checked={selected.includes(candidate.name)} onChange={event => {
              setReview(null); setError(false); setSaved(false);
              setSelected(previous => event.target.checked ? [...previous, candidate.name] : previous.filter(name => name !== candidate.name));
            }} /> <span>{candidate.title}</span> <code>{candidate.name}</code></label>)}
        </div>
      </fieldset>
      <p>{tx("定义、配置或发现结果变化后，需要重新检查；服务器撤回提示或修改参数定义时，自动恢复调用确认。", "Review again when definitions, configuration, or discovery results change. A withdrawn hint or changed input schema restores per-call confirmation.")}</p>
      <div className="ae-inline-actions">
        <button type="button" className="ae-btn ae-btn-primary" disabled={busy || !canReview || !selected.length} onClick={() => {
          setReview({ anchor, payload: { expected_revision: server.revision, instance_id: server.instance_id!, discovery_revision: server.discovery_revision!,
            tools: candidates.filter(candidate => selected.includes(candidate.name)).map(({ name, schema_sha256 }) => ({ name, schema_sha256 })) } });
        }}>{tx("检查只读设置", "Review read-only settings")}</button>
      </div>
      {review?.anchor === anchor ? <div className="ae-confirm-dialog" role="alertdialog" aria-label={tx("确认只读工具", "Confirm read-only tools")}>
        <p>{tx("是否将这些工具设为只读，并跳过之后每次调用的确认？", "Mark these tools read-only and skip confirmation for their later calls?")}</p>
        <ul className="ae-confirm-tool-list">{review.payload.tools.map(item => <li key={item.name}><code>{item.name}</code></li>)}</ul>
        <div className="ae-inline-actions">
          <button type="button" className="ae-btn ae-btn-primary" disabled={busy} onClick={() => void apply()}>{tx("确认采用只读提示", "Confirm read-only hints")}</button>
          <button type="button" className="ae-btn" disabled={busy} onClick={() => { setReview(null); setSelected([]); }}>{tx("取消", "Cancel")}</button>
        </div>
      </div> : null}
    </section> : null}
    {error ? <p role="alert">{tx("只读设置未应用。请刷新详情，重新检查工具后再确认。", "Read-only settings were not applied. Refresh the details and review the tools again.")}</p> : null}
    {saved ? <p role="status">{tx("已应用选中的只读提示。", "Selected read-only hints applied.")}</p> : null}
  </>;
}
