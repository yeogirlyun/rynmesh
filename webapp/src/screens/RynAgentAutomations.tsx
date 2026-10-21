import React, { useEffect, useState } from "react";
import { AgentAutomation, AgentEvent, rynAgent } from "../domain/rynAgent";

export function extractTurnAutomations(events: AgentEvent[]): AgentAutomation[] {
  const out: AgentAutomation[] = [];
  const seen = new Set<string>();
  for (const ev of events) {
    if (ev.type !== "tool_result" || ev.tool !== "ryn_automation_update") continue;
    const res = ev.result && typeof ev.result === "object" ? (ev.result as Record<string, unknown>) : null;
    if (!res || res.ok === false) continue;
    const candidate = (res.automation && typeof res.automation === "object"
      ? res.automation
      : res.id && res.name
        ? res
        : null) as AgentAutomation | null;
    if (candidate && typeof candidate.id === "string" && !seen.has(candidate.id)) {
      seen.add(candidate.id);
      out.push(candidate);
    }
  }
  return out;
}

interface PresetTemplate {
  icon: string;
  nameZh: string;
  nameEn: string;
  descZh: string;
  descEn: string;
  promptZh: string;
  promptEn: string;
  repeat: AgentAutomation["repeat"];
  time: string;
}

const PRESET_TEMPLATES: PresetTemplate[] = [
  {
    icon: "💬",
    nameZh: "给好友发微信消息",
    nameEn: "Send WeChat Message",
    descZh: "每天固定时间自动打开微信发问候或提醒",
    descEn: "Open WeChat and send a message at a set time",
    promptZh: "打开桌面微信客户端，搜索联系人「张三」，在聊天窗口输入并发送消息：「早点休息」",
    promptEn: "Open WeChat desktop client, search for contact 'Zhang San', and send message: 'Good night!'",
    repeat: "daily",
    time: "22:00",
  },
  {
    icon: "☀️",
    nameZh: "每日工作早报汇总",
    nameEn: "Morning Daily Brief",
    descZh: "每个工作日早上整理今日待办与重点消息",
    descEn: "Summarize today's todos and priorities every weekday morning",
    promptZh: "检查当前工作区近期任务和未读事项，为我生成一份简明扼要的今日待办清单。",
    promptEn: "Check recent workspace tasks and unread items, and generate a concise daily todo brief.",
    repeat: "weekdays",
    time: "09:00",
  },
  {
    icon: "🗂️",
    nameZh: "桌面与下载文件夹整理",
    nameEn: "Tidy Downloads Folder",
    descZh: "每周五下午自动归档零散文件",
    descEn: "Organize scattered files every Friday afternoon",
    promptZh: "检查下载目录中的近期文件，按文档、图片、安装包分类整理并汇报结果。",
    promptEn: "Inspect recent files in Downloads, categorize them by type, and report a summary.",
    repeat: "weekly",
    time: "17:00",
  },
  {
    icon: "🔔",
    nameZh: "晚间复盘与次日提醒",
    nameEn: "Evening Wrap-up",
    descZh: "每天晚上汇总今日完成进度",
    descEn: "Summarize completed work every evening",
    promptZh: "回顾今天完成的任务与改动，生成一段简短的今日总结。",
    promptEn: "Review tasks and changes completed today and write a short evening summary.",
    repeat: "daily",
    time: "21:30",
  },
];

const WEEKDAYS_LIST: Array<{ code: string; zh: string; en: string }> = [
  { code: "MO", zh: "周一", en: "Mon" },
  { code: "TU", zh: "周二", en: "Tue" },
  { code: "WE", zh: "周三", en: "Wed" },
  { code: "TH", zh: "周四", en: "Thu" },
  { code: "FR", zh: "周五", en: "Fri" },
  { code: "SA", zh: "周六", en: "Sat" },
  { code: "SU", zh: "周日", en: "Sun" },
];

export function formatAutomationSchedule(item: Partial<AgentAutomation>, zh: boolean): string {
  if (zh && item.schedule_label_zh) return item.schedule_label_zh;
  if (!zh && item.schedule_label_en) return item.schedule_label_en;
  if (item.schedule_label) return item.schedule_label;
  const t = item.time || "22:00";
  switch (item.repeat) {
    case "weekdays":
      return zh ? `工作日 ${t}` : `Weekdays at ${t}`;
    case "weekly":
      return zh ? `每周 ${t}` : `Weekly at ${t}`;
    case "hourly":
      return zh ? `每 ${item.interval_hours || 1} 小时` : `Every ${item.interval_hours || 1}h`;
    case "once":
      return zh ? `仅一次 ${t}` : `Once at ${t}`;
    default:
      return zh ? `每天 ${t}` : `Daily at ${t}`;
  }
}

export function AutomationChatCard({
  automation,
  zh,
  onEdit,
  onRunNow,
}: {
  automation: AgentAutomation;
  zh: boolean;
  onEdit: (item: AgentAutomation) => void;
  onRunNow?: (item: AgentAutomation) => void;
}) {
  const paused = automation.status === "PAUSED";
  const label = formatAutomationSchedule(automation, zh);
  return (
    <div className={`ra-auto-chat-card${paused ? " is-paused" : ""}`}>
      <div className="ra-auto-chat-card-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2">
          <circle cx="12" cy="12" r="9" />
          <path d="M12 7v5l3 2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </div>
      <div className="ra-auto-chat-card-body" onClick={() => onEdit(automation)}>
        <div className="ra-auto-chat-card-title">
          <span>{automation.name || (zh ? "定时任务" : "Scheduled Task")}</span>
          {paused && (
            <span className="ra-auto-status-pill is-paused">
              {zh ? "已暂停" : "Paused"}
            </span>
          )}
        </div>
        <div className="ra-auto-chat-card-sub">{label}</div>
      </div>
      <div className="ra-auto-chat-card-actions">
        {onRunNow && (
          <button
            type="button"
            className="ra-auto-card-btn"
            title={zh ? "立即试运行一次" : "Run now"}
            onClick={() => onRunNow(automation)}
          >
            {zh ? "试运行" : "Run"}
          </button>
        )}
        <button
          type="button"
          className="ra-auto-card-icon-btn"
          title={zh ? "修改时间或内容" : "Edit schedule"}
          onClick={() => onEdit(automation)}
        >
          <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M12 20h9" strokeLinecap="round" />
            <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </div>
    </div>
  );
}

export function AutomationDialog({
  open,
  initial,
  zh,
  onClose,
  onSaved,
  onDeleted,
}: {
  open: boolean;
  initial: Partial<AgentAutomation> | null;
  zh: boolean;
  onClose: () => void;
  onSaved: (saved: AgentAutomation) => void;
  onDeleted?: (id: string) => void;
}) {
  const isEditing = Boolean(initial && initial.id);
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [repeat, setRepeat] = useState<AgentAutomation["repeat"]>("daily");
  const [time, setTime] = useState("22:00");
  const [weekday, setWeekday] = useState("FR");
  const [status, setStatus] = useState<"ACTIVE" | "PAUSED">("ACTIVE");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    setName(initial?.name || "");
    setPrompt(initial?.prompt || "");
    setRepeat((initial?.repeat as AgentAutomation["repeat"]) || "daily");
    setTime(initial?.time || "22:00");
    setWeekday(initial?.weekday || "FR");
    setStatus(initial?.status === "PAUSED" ? "PAUSED" : "ACTIVE");
    setError("");
  }, [open, initial]);

  if (!open) return null;

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    const cleanPrompt = prompt.trim();
    const cleanName = name.trim() || cleanPrompt.slice(0, 24) || (zh ? "定时任务" : "Scheduled Task");
    if (!cleanPrompt) {
      setError(zh ? "请填写到了时间要执行的内容" : "Please enter what the agent should do");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const payload: Record<string, unknown> = {
        name: cleanName,
        prompt: cleanPrompt,
        status,
        desktop_access: initial?.desktop_access ?? true,
      };
      const scheduleChanged = !isEditing
        || repeat !== (initial?.repeat || "daily")
        || time !== (initial?.time || "22:00")
        || (repeat === "weekly" && weekday !== (initial?.weekday || "FR"));
      if (scheduleChanged) {
        Object.assign(payload, { repeat, time, weekday });
        // The form displays one weekday; preserve additional days when only
        // changing the time of an existing weekly schedule.
        if (repeat === "weekly" && initial?.repeat === "weekly"
          && weekday === (initial.weekday || "FR") && initial.weekdays?.length) {
          payload.weekdays = initial.weekdays;
        }
      }
      const res = isEditing && initial?.id
        ? await rynAgent.updateAutomation(initial.id, payload)
        : await rynAgent.createAutomation(payload);
      onSaved(res.automation);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!isEditing || !initial?.id) return;
    setSaving(true);
    try {
      await rynAgent.deleteAutomation(initial.id);
      onDeleted?.(initial.id);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="ra-modal-backdrop" onClick={onClose}>
      <div className="ra-auto-dialog" onClick={e => e.stopPropagation()}>
        <form onSubmit={handleSave}>
          <div className="ra-auto-dialog-header">
            <input
              type="text"
              className="ra-auto-title-input"
              placeholder={zh ? "任务名称（如：给张三发微信消息）" : "Task title (e.g. Send WeChat message)"}
              value={name}
              onChange={e => setName(e.target.value)}
              autoFocus
            />
            {isEditing && (
              <button
                type="button"
                className={`ra-auto-status-toggle ${status === "ACTIVE" ? "is-active" : "is-paused"}`}
                onClick={() => setStatus(s => (s === "ACTIVE" ? "PAUSED" : "ACTIVE"))}
              >
                {status === "ACTIVE" ? (zh ? "● 已启用" : "● Active") : (zh ? "○ 已暂停" : "○ Paused")}
              </button>
            )}
          </div>

          <div className="ra-auto-field">
            <label className="ra-auto-label">
              {zh ? "到了时间让 AI 做什么？" : "What should the AI do?"}
            </label>
            <textarea
              className="ra-auto-textarea"
              rows={3}
              placeholder={
                zh
                  ? "直接用大白话写，例如：打开微信客户端，搜索联系人「张三」，发送消息：「早点休息」"
                  : "Describe in plain words, e.g. Open WeChat, search for 'Zhang San', and send 'Good night'"
              }
              value={prompt}
              onChange={e => setPrompt(e.target.value)}
            />
          </div>

          <div className="ra-auto-schedule-row">
            <div className="ra-auto-field">
              <label className="ra-auto-label">{zh ? "重复频率" : "Repeat"}</label>
              <select
                className="ra-auto-select"
                value={repeat}
                onChange={e => setRepeat(e.target.value as AgentAutomation["repeat"])}
              >
                <option value="daily">{zh ? "每天" : "Every day"}</option>
                <option value="weekdays">{zh ? "工作日 (周一至周五)" : "Weekdays (Mon–Fri)"}</option>
                <option value="weekly">{zh ? "每周" : "Weekly"}</option>
                <option value="once">{zh ? "仅一次" : "Once"}</option>
              </select>
            </div>

            {repeat === "weekly" && (
              <div className="ra-auto-field">
                <label className="ra-auto-label">{zh ? "星期" : "Day"}</label>
                <select
                  className="ra-auto-select"
                  value={weekday}
                  onChange={e => setWeekday(e.target.value)}
                >
                  {WEEKDAYS_LIST.map(d => (
                    <option key={d.code} value={d.code}>
                      {zh ? d.zh : d.en}
                    </option>
                  ))}
                </select>
              </div>
            )}

            <div className="ra-auto-field">
              <label className="ra-auto-label">{zh ? "执行时间" : "Time"}</label>
              <input
                type="time"
                className="ra-auto-time-input"
                value={time}
                onChange={e => setTime(e.target.value || "22:00")}
              />
            </div>
          </div>

          {error && <div className="ra-auto-error">{error}</div>}

          <div className="ra-auto-dialog-footer">
            <div>
              {isEditing && (
                <button
                  type="button"
                  className="ra-auto-delete-btn"
                  disabled={saving}
                  onClick={handleDelete}
                >
                  {zh ? "删除任务" : "Delete"}
                </button>
              )}
            </div>
            <div className="ra-auto-footer-right">
              <button type="button" className="ra-auto-cancel-btn" onClick={onClose}>
                {zh ? "取消" : "Cancel"}
              </button>
              <button type="submit" className="ra-auto-save-btn" disabled={saving}>
                {saving ? (zh ? "保存中..." : "Saving...") : (zh ? "保存" : "Save")}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}

export function AutomationsPanel({
  automations,
  zh,
  onRefresh,
  onEdit,
  onCreateNew,
  onSelectTask,
}: {
  automations: AgentAutomation[];
  zh: boolean;
  onRefresh: () => void;
  onEdit: (item: Partial<AgentAutomation>) => void;
  onCreateNew: () => void;
  onSelectTask?: (taskId: string) => void;
}) {
  const [filter, setFilter] = useState<"ACTIVE" | "PAUSED">("ACTIVE");
  const [busyId, setBusyId] = useState<string | null>(null);

  const activeItems = automations.filter(a => a.status !== "PAUSED");
  const pausedItems = automations.filter(a => a.status === "PAUSED");
  const shown = filter === "ACTIVE" ? activeItems : pausedItems;

  const handleToggleStatus = async (item: AgentAutomation) => {
    setBusyId(item.id);
    try {
      const nextStatus = item.status === "PAUSED" ? "ACTIVE" : "PAUSED";
      await rynAgent.updateAutomation(item.id, { status: nextStatus });
      onRefresh();
    } finally {
      setBusyId(null);
    }
  };

  const handleRunNow = async (item: AgentAutomation) => {
    setBusyId(item.id);
    try {
      const res = await rynAgent.runAutomationNow(item.id);
      onRefresh();
      if (res.task_id && onSelectTask) {
        onSelectTask(res.task_id);
      }
    } finally {
      setBusyId(null);
    }
  };

  const handleDelete = async (item: AgentAutomation) => {
    setBusyId(item.id);
    try {
      await rynAgent.deleteAutomation(item.id);
      onRefresh();
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="ra-auto-page">
      <div className="ra-auto-page-inner">
        <div className="ra-auto-page-header">
          <div>
            <h2 className="ra-auto-page-title">{zh ? "定时任务" : "Scheduled Tasks"}</h2>
            <p className="ra-auto-page-sub">
              {zh
                ? "到了设定时间自动帮你执行（也可以直接在聊天里说「每天晚上十点给谁发微信」）"
                : "Automatically run tasks on schedule, or just ask in chat."}
            </p>
          </div>
          <button type="button" className="ra-auto-new-btn" onClick={onCreateNew}>
            + {zh ? "新建定时任务" : "New"}
          </button>
        </div>

        <div className="ra-auto-filter-bar">
          <button
            type="button"
            className={`ra-auto-filter-tab${filter === "ACTIVE" ? " is-active" : ""}`}
            onClick={() => setFilter("ACTIVE")}
          >
            {zh ? "运行中" : "Active"} <span>{activeItems.length}</span>
          </button>
          <button
            type="button"
            className={`ra-auto-filter-tab${filter === "PAUSED" ? " is-active" : ""}`}
            onClick={() => setFilter("PAUSED")}
          >
            {zh ? "已暂停" : "Paused"} <span>{pausedItems.length}</span>
          </button>
        </div>

        <div className="ra-auto-list">
          {shown.length === 0 ? (
            <div className="ra-auto-empty">
              {filter === "ACTIVE"
                ? zh
                  ? "还没有运行中的定时任务。点击下方模板一键创建，或直接回聊天告诉 AI。"
                  : "No active scheduled tasks yet. Pick a template below or ask in chat."
                : zh
                  ? "没有已暂停的定时任务。"
                  : "No paused scheduled tasks."}
            </div>
          ) : (
            shown.map(item => (
              <div
                key={item.id}
                className={`ra-auto-row${item.status === "PAUSED" ? " is-paused" : ""}`}
                onClick={() => onEdit(item)}
              >
                <div className="ra-auto-row-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2">
                    <circle cx="12" cy="12" r="9" />
                    <path d="M12 7v5l3 2" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </div>
                <div className="ra-auto-row-main">
                  <span className="ra-auto-row-name">{item.name}</span>
                  <span className="ra-auto-row-prompt">{item.prompt}</span>
                  {item.last_run_summary && (
                    <span
                      className="ra-auto-row-last"
                      onClick={e => {
                        if (item.last_task_id && onSelectTask) {
                          e.stopPropagation();
                          onSelectTask(item.last_task_id);
                        }
                      }}
                    >
                      {zh ? "上次：" : "Last: "}
                      {item.last_run_summary}
                    </span>
                  )}
                </div>
                <div className="ra-auto-row-right" onClick={e => e.stopPropagation()}>
                  <span className="ra-auto-row-time">{formatAutomationSchedule(item, zh)}</span>
                  <div className="ra-auto-row-actions">
                    <button
                      type="button"
                      className="ra-auto-row-btn"
                      disabled={busyId === item.id}
                      onClick={() => handleRunNow(item)}
                    >
                      {zh ? "立即运行" : "Run now"}
                    </button>
                    <button
                      type="button"
                      className="ra-auto-row-btn"
                      disabled={busyId === item.id}
                      onClick={() => handleToggleStatus(item)}
                    >
                      {item.status === "PAUSED" ? (zh ? "恢复" : "Resume") : (zh ? "暂停" : "Pause")}
                    </button>
                    <button
                      type="button"
                      className="ra-auto-row-btn is-danger"
                      disabled={busyId === item.id}
                      onClick={() => handleDelete(item)}
                    >
                      {zh ? "删除" : "Delete"}
                    </button>
                  </div>
                </div>
              </div>
            ))
          )}
        </div>

        <div className="ra-auto-suggestions">
          <div className="ra-auto-suggestions-title">{zh ? "一键模板" : "Quick Templates"}</div>
          <div className="ra-auto-suggestions-grid">
            {PRESET_TEMPLATES.map((tpl, idx) => (
              <button
                key={idx}
                type="button"
                className="ra-auto-suggestion-card"
                onClick={() =>
                  onEdit({
                    name: zh ? tpl.nameZh : tpl.nameEn,
                    prompt: zh ? tpl.promptZh : tpl.promptEn,
                    repeat: tpl.repeat,
                    time: tpl.time,
                    status: "ACTIVE",
                  })
                }
              >
                <div className="ra-auto-suggestion-top">
                  <span className="ra-auto-suggestion-icon">{tpl.icon}</span>
                  <span className="ra-auto-suggestion-name">{zh ? tpl.nameZh : tpl.nameEn}</span>
                </div>
                <div className="ra-auto-suggestion-desc">{zh ? tpl.descZh : tpl.descEn}</div>
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
