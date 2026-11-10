import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useInRouterContext } from "react-router-dom";
import { AgentAPIError, rynAgent, type AgentPreferences, type AgentSkill } from "../domain/rynAgent";

function useText() {
  const { i18n } = useTranslation();
  return (cn: string, en: string) => i18n.language.startsWith("zh") ? cn : en;
}
const labels: Record<string, [string, string]> = {
  "system-diagnostics": ["系统诊断", "System diagnostics"],
  "resource-report": ["资料报告", "Report from resources"],
  "source-configuration": ["模型来源配置", "Source configuration"],
};

export function TaskSkills({ skills }: { skills?: AgentSkill[] }) {
  const text = useText();
  if (!skills?.length) return null;
  return <details className="ra-task-context"><summary>{text("本任务技能", "Skills for this task")} · {skills.length}</summary>
    <p>{text("使用创建任务时选定的技能版本。技能不增加权限；新任务可重新选择。", "Uses the skill versions selected when this task was created. Skills grant no permissions; choose again for a new task.")}</p>
    <ul>{skills.map(skill => <li key={skill.id}>{labels[skill.id] ? text(...labels[skill.id]) : skill.title} · v{skill.version}</li>)}</ul>
  </details>;
}

export function SkillSelection({ value, onChange, disabled }: { value: string[]; onChange: (value: string[]) => void; disabled: boolean }) {
  const text = useText();
  const inRouter = useInRouterContext();
  const [open, setOpen] = useState(false);
  const [skills, setSkills] = useState<AgentSkill[] | null>(null);
  const [error, setError] = useState(false);
  const load = async () => {
    setError(false);
    try { setSkills((await rynAgent.skills()).skills); } catch { setError(true); }
  };
  const manageLabel = text("管理技能", "Manage skills");
  return <div className="ra-guidance">
    <button className="ra-link" aria-expanded={open} onClick={() => { setOpen(!open); if (!open && !skills) void load(); }}>{text("技能", "Skills")}{value.length ? ` · ${value.length}` : ` · ${text("可选", "optional")}`}</button>
    {open && <fieldset className="ra-tool-permissions"><legend>{text("为新任务选择技能", "Choose skills for the new task")}</legend>
      <p>{text("技能提供处理步骤，不会增加工具权限。只对新任务生效。", "Skills offer workflow guidance without granting tool access. They apply to the new task only.")}</p>
      {error ? <button className="ra-link" onClick={() => void load()}>{text("加载失败，重试", "Load failed — retry")}</button> : skills === null ? <p role="status">{text("正在加载…", "Loading…")}</p> : skills.map(skill => <label key={skill.id}><input type="checkbox" checked={value.includes(skill.id)} disabled={disabled} onChange={event => onChange(event.target.checked ? [...value, skill.id] : value.filter(id => id !== skill.id))} /><span>{labels[skill.id] ? text(...labels[skill.id]) : skill.title}<small>v{skill.version}</small></span></label>)}
      <div className="ra-skill-manage-row">
        {inRouter ? <Link className="ra-link" to="/settings/extensions/skills">{manageLabel}</Link> : <a className="ra-link" href="/settings/extensions/skills">{manageLabel}</a>}
      </div>
    </fieldset>}
  </div>;
}

export function PreferencesEditor() {
  const text = useText();
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState<AgentPreferences | null>(null);
  const [items, setItems] = useState<AgentPreferences["items"]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [needsReload, setNeedsReload] = useState(false);
  const pending = useRef<{ expected_revision: number; items: AgentPreferences["items"] } | null>(null);
  const load = async () => {
    setBusy(true); setError("");
    try { const loaded = await rynAgent.preferences(); setCurrent(loaded); setItems(loaded.items); setNeedsReload(false); pending.current = null; }
    catch { setError(text("无法读取偏好，请重试。", "Unable to load preferences. Retry.")); }
    finally { setBusy(false); }
  };
  const save = async () => {
    if (!current || busy || needsReload) return;
    pending.current ??= { expected_revision: current.revision, items: structuredClone(items) };
    setBusy(true); setError("");
    try {
      const updated = await rynAgent.savePreferences(pending.current);
      setCurrent(updated); setItems(updated.items); pending.current = null;
    } catch (reason) {
      if (reason instanceof AgentAPIError && reason.status === 409) {
        setNeedsReload(true);
        setError(text("偏好已变化，或上次保存已生效。请重新读取后核对；不会覆盖新版本。", "Preferences changed, or the previous save took effect. Reload and review; the newer version will not be overwritten."));
      } else setError(text("保存结果未确认。重试会保持原版本与内容，也可以重新读取。", "Save is unconfirmed. Retry keeps the original version and content, or reload to review."));
    } finally { setBusy(false); }
  };
  const locked = busy || Boolean(pending.current) || needsReload;
  return <div className="ra-guidance">
    <button className="ra-link" aria-expanded={open} onClick={() => { setOpen(!open); if (!open && !current) void load(); }}>{text("我的偏好", "My preferences")}</button>
    {open && <section aria-label={text("本机偏好", "Local preferences")}>
      <p>{text("由你明确保存，在后续模型调用中使用。当前任务要求优先，偏好不能授权操作。不要填写密钥。", "Saved explicitly by you and used in subsequent model calls. Current task requirements take priority; preferences grant no permissions. Do not enter secrets.")}</p>
      {items.map((item, index) => <div className="ra-preference-row" key={item.id}><textarea aria-label={`${text("偏好", "Preference")} ${index + 1}`} value={item.text} maxLength={1000} disabled={locked} onChange={event => setItems(old => old.map(row => row.id === item.id ? { ...row, text: event.target.value } : row))} /><button className="ra-link" disabled={locked} onClick={() => setItems(old => old.filter(row => row.id !== item.id))}>{text("移除", "Remove")}</button></div>)}
      {error && <p role="alert">{error}</p>}
      <div className="ra-approval-actions"><button className="ra-link" disabled={!current || locked || items.length >= 16} onClick={() => setItems(old => [...old, { id: crypto.randomUUID(), text: "" }])}>{text("添加偏好", "Add preference")}</button>
        <button className="ra-link" disabled={busy} onClick={() => void load()}>{text("重新读取", "Reload")}</button>
        <button className="ra-primary" disabled={!current || busy || needsReload || items.some(item => !item.text.trim())} onClick={() => void save()}>{pending.current ? text("重试原保存", "Retry original save") : text("保存偏好", "Save preferences")}</button></div>
    </section>}
  </div>;
}

export { MemoryEditor, type MemoryEditorProps } from "./RynAgentMemory";

