import { useState } from "react";
import { useTranslation } from "react-i18next";
import { AgentAPIError, canUseAgentAction, rynAgent, type AgentBudgetLimits, type AgentTask } from "../domain/rynAgent";

type Request = { limits: AgentBudgetLimits; run_max_seconds?: number; expected_control_revision: number; client_request_id: string };
const ranges: Record<keyof AgentBudgetLimits, [number, number]> = { max_total_model_calls: [1, 1024], max_total_tool_calls: [1, 4096], max_total_tokens: [1000, 20000000], max_total_seconds: [10, 86400] };
function readPending(key: string): Request | null {
  try { const item = JSON.parse(sessionStorage.getItem(key) || "null"); return item && typeof item.client_request_id === "string" && typeof item.expected_control_revision === "number" && item.limits ? item : null; } catch { return null; }
}

export default function RynAgentBudget({ task, busy, onChange }: { task: AgentTask; busy: boolean; onChange: (task: AgentTask) => void }) {
  const { i18n } = useTranslation();
  const text = (cn: string, en: string) => i18n.language.startsWith("zh") ? cn : en;
  const key = `ryn.agent.pending-budget.${task.id}`;
  const [pending, setPending] = useState<Request | null>(() => readPending(key));
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<AgentBudgetLimits | null>(null);
  const [runSeconds, setRunSeconds] = useState<number | null>(null);
  const [revision, setRevision] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  if (!task.budget) return null;
  const data = task.budget;
  const labels: Record<keyof AgentBudgetLimits, string> = {
    max_total_model_calls: text("模型请求", "Model requests"), max_total_tool_calls: text("工具调用", "Tool calls"),
    max_total_tokens: "Token", max_total_seconds: text("执行时间（秒）", "Execution time (seconds)"),
  };
  const allowed = canUseAgentAction(task, "budget");
  const valid = draft && (runSeconds === null || (Number.isInteger(runSeconds) && runSeconds >= 10 && runSeconds <= 3600)) && Object.entries(ranges).every(([name, [min, max]]) => Number.isInteger(draft[name as keyof AgentBudgetLimits]) && draft[name as keyof AgentBudgetLimits] >= min && draft[name as keyof AgentBudgetLimits] <= max);
  const remember = (value: Request | null) => { setPending(value); try { if (value) sessionStorage.setItem(key, JSON.stringify(value)); else sessionStorage.removeItem(key); } catch { /* Memory retry remains available. */ } };
  const save = async () => {
    if (busy || saving || (!pending && (!valid || !allowed || revision === null))) return;
    const body = pending || { limits: draft!, ...(runSeconds === null ? {} : { run_max_seconds: runSeconds }), expected_control_revision: revision!, client_request_id: crypto.randomUUID() };
    remember(body); setSaving(true); setError("");
    try { const updated = await rynAgent.updateBudget(task.id, body); remember(null); setEditing(false); onChange(updated); }
    catch (reason) {
      if (reason instanceof AgentAPIError && reason.status >= 400 && reason.status < 500) {
        remember(null); setEditing(false);
        setError(text("任务状态或额度已变化，请核对最新记录后重新编辑。", "The task state or limits changed. Review the current record before editing again."));
      } else setError(text("保存结果未确认。仅重试原版本和原额度，不会自动继续任务。", "Save is unconfirmed. Retry uses the original version and limits; it does not resume the task."));
    } finally { setSaving(false); }
  };
  const known = (value: number | null | undefined) => value == null ? text("未知", "Unknown") : value > 0 && value < 0.01 ? "< 0.01" : value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  const timing = task.run_timing;
  const values = [
    [labels.max_total_model_calls, known(data.current_round.model_calls), known(data.lifetime.model_calls), known(data.limits.max_total_model_calls)],
    [labels.max_total_tool_calls, known(data.current_round.tool_calls), known(data.lifetime.tool_calls), known(data.limits.max_total_tool_calls)],
    ["Token", known(data.current_round.total_tokens), known(data.lifetime.total_tokens), known(data.limits.max_total_tokens)],
    [text("模型与工具调用时间（秒）", "Model and tool call time (seconds)"), known(data.current_round.active_seconds), known(data.lifetime.active_seconds), known(data.limits.max_total_seconds)],
  ];
  const reasons: Record<string, string> = {
    agent_model_call_budget_exhausted: text("模型请求次数已用完", "Model request allowance exhausted"),
    agent_tool_budget_exhausted: text("工具调用次数已用完", "Tool call allowance exhausted"),
    agent_token_budget_exhausted: text("剩余 Token 额度不足以发起下一次请求", "Remaining token allowance cannot cover the next request"),
    agent_time_budget_exhausted: text("任务累计执行时间已用完", "Lifetime execution time exhausted"),
    agent_step_limit: text("本轮请求次数达到上限", "This round's request limit was reached"),
    agent_time_limit: text("本次运行时间达到上限", "The run time limit was reached"),
  };
  return <details className="ra-budget" open={pending || editing || Boolean(reasons[task.error || ""]) ? true : undefined}>
    <summary>{text("任务预算与用量", "Task budget and usage")}</summary>
    {reasons[task.error || ""] && <p role="status">{reasons[task.error!]}</p>}
    <p>{text("本次运行时限（秒）", "Run time limit (seconds)")}: {known(timing?.elapsed_seconds)} / {known(timing?.max_seconds ?? task.limits?.max_seconds)} · {timing?.measurement === "live" ? text("本次查询快照", "Snapshot at this query") : text("已记录时间", "Recorded time")}</p>
    <p>{text("运行时限包含请求准备、校验和模型/工具执行；等待审批、等待输入、暂停和排队不计时。正在停止的操作仍计时。", "The run limit includes request preparation, validation, and model/tool execution; waiting for approval or input, pauses, and queue time are excluded. An operation that is still stopping continues to count.")}</p>
    {timing?.measurement === "pending" && <p>{text("当前操作尚未结算，显示值未包含这段时间。", "The current operation has not settled; its time is not included in the displayed value.")}</p>}
    {!timing || timing.measurement === "unknown" ? <p>{text("此记录的运行计时未知，不能从调用时间补算。", "Run timing is unknown for this record; it cannot be reconstructed from call time.")}</p> : <p>{text("暂停或等待输入后继续沿用已记录计时；重启前未结算的时间不会推算补入。", "Continuing after a pause or input request retains recorded timing; unsettled time before a restart is not reconstructed.")}</p>}
    <div className="ra-budget-table"><table><thead><tr><th>{text("项目", "Metric")}</th><th>{text("本轮", "This round")}</th><th>{text("累计", "Lifetime")}</th><th>{text("累计上限", "Lifetime limit")}</th></tr></thead><tbody>{values.map(row => <tr key={row[0]}>{row.map((cell, index) => index === 0 ? <th key={index}>{cell}</th> : <td key={index}>{cell}</td>)}</tr>)}</tbody></table></div>
    <p>{text("用于限制后续请求的已占额度", "Allowance charged for admission")}: {known(data.lifetime.charged_tokens)} Token · {known(data.lifetime.charged_seconds)} {text("秒", "seconds")}</p>
    {data.lifetime.cached_input_tokens != null && data.lifetime.cached_input_tokens > 0 && <p>{text("缓存命中拆分", "Cached input breakdown")}: {text("未缓存输入", "Uncached input")} {known(Math.max(0, (data.lifetime.input_tokens ?? data.lifetime.known_input_tokens ?? 0) - data.lifetime.cached_input_tokens))} · {text("缓存命中", "Cached input")} {known(data.lifetime.cached_input_tokens)} · {text("输出", "Output")} {known(data.lifetime.output_tokens ?? data.lifetime.known_output_tokens ?? 0)}（{text("直连路由按缓存折扣计入已占额度", "Direct routes discount cached input in charged allowance")}）</p>}
    {!data.lifetime.usage_complete && <p>{text("部分实际用量未知，按派发前预留额度保守计入。未知不等于零。", "Some measured usage is unknown; pre-dispatch reservations remain charged conservatively. Unknown does not mean zero.")}</p>}
    {data.lifetime.legacy_usage_unknown && <p>{text("旧版本记录不完整；累计调用至少为", "Legacy records are incomplete; known lifetime call floors are")}: {data.lifetime.charged_model_calls ?? 0} {text("次模型请求、", "model requests,")} {data.lifetime.charged_tool_calls ?? 0} {text("次工具调用。历史未记录的 Token 无法补算。", "tool calls. Unrecorded historical tokens cannot be reconstructed.")}</p>}
    <p>{text("费用：未知。继续或重启不会清空累计用量；保存新上限不会自动继续任务。", "Cost: unknown. Continuing or restarting preserves lifetime usage; saving limits does not resume the task.")}</p>
    {(editing || pending) && <div className="ra-budget-inputs">{(Object.keys(ranges) as Array<keyof AgentBudgetLimits>).map(name => <label key={name}><span>{labels[name]}</span><input type="number" min={ranges[name][0]} max={ranges[name][1]} step={1} value={(pending?.limits || draft)?.[name] ?? ""} disabled={saving || busy || Boolean(pending)} onChange={event => setDraft(current => current ? { ...current, [name]: Number(event.target.value) } : current)} /></label>)}</div>}
    {error && <p role="alert">{error}</p>}
    {(editing || pending) && (runSeconds !== null || pending?.run_max_seconds !== undefined) && <label className="ra-setting"><span>{text("单轮运行时限（秒）", "Run limit (seconds)")}</span><input type="number" min={10} max={3600} step={1} value={pending?.run_max_seconds ?? runSeconds ?? ""} disabled={saving || busy || Boolean(pending)} onChange={event => setRunSeconds(Number(event.target.value))} /></label>}
    <div className="ra-approval-actions">{editing || pending ? <><button className="ra-primary" disabled={busy || saving || (!pending && (!valid || !allowed))} onClick={() => void save()}>{pending ? text("核对原保存请求", "Check original save") : text("保存预算", "Save limits")}</button>{!pending && <button className="ra-link" disabled={saving} onClick={() => setEditing(false)}>{text("取消编辑", "Cancel editing")}</button>}</> : <button className="ra-link" disabled={busy || !allowed} onClick={() => { setDraft({ ...data.limits }); setRunSeconds(task.limits?.max_seconds ?? null); setRevision(task.control_revision!); setEditing(true); setError(""); }}>{text("调整预算", "Adjust limits")}</button>}</div>
    {!allowed && !pending && <small>{text("任务停止且没有待处理操作时才可调整。", "Limits can be adjusted when the task is stopped with no unresolved operation.")}</small>}
  </details>;
}
