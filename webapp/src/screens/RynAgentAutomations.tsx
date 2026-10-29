import React, { useEffect, useMemo, useState } from "react";
import {
  AgentAutomation,
  AgentAutomationRun,
  AgentEvent,
  AgentTaskSummary,
  rynAgent,
} from "../domain/rynAgent";

export function extractTurnAutomations(events: AgentEvent[]): AgentAutomation[] {
  const out: AgentAutomation[] = [];
  const seen = new Set<string>();
  for (const ev of events) {
    if (ev.type !== "tool_result" || ev.tool !== "ryn_automation_update") continue;
    const res =
      ev.result && typeof ev.result === "object"
        ? (ev.result as Record<string, unknown>)
        : null;
    if (!res || res.ok === false) continue;
    const candidate = (
      res.automation && typeof res.automation === "object"
        ? res.automation
        : res.id && res.name
          ? res
          : null
    ) as AgentAutomation | null;
    if (candidate && typeof candidate.id === "string" && !seen.has(candidate.id)) {
      seen.add(candidate.id);
      out.push(candidate);
    }
  }
  return out;
}

interface CodexSuggestion {
  id: string;
  iconBg: string;
  iconColor: string;
  iconType: "book" | "chart" | "shield";
  nameZh: string;
  nameEn: string;
  descZh: string;
  descEn: string;
  promptZh: string;
  promptEn: string;
  scheduleZh: string;
  scheduleEn: string;
  kind: "heartbeat" | "cron";
  repeat: AgentAutomation["repeat"];
  time: string;
  weekdays?: string[];
}

const CODEX_SUGGESTIONS: CodexSuggestion[] = [
  {
    id: "daily-brief",
    iconBg: "#EFF6FF",
    iconColor: "#2563EB",
    iconType: "book",
    nameZh: "每日简报",
    nameEn: "Daily brief",
    descZh: "总结当日关键更新、未读消息与优先待办概览",
    descEn: "Start each weekday with a summary of updates and priorities",
    promptZh: "给我一份晨间简报，汇总近期任务进展、重要变更以及今天需要我优先关注的事项。",
    promptEn: "Give me a morning brief with recent task progress and anything that needs my attention today.",
    scheduleZh: "工作日 08:00",
    scheduleEn: "Weekdays 08:00",
    kind: "cron",
    repeat: "weekdays",
    time: "08:00",
    weekdays: ["MO", "TU", "WE", "TH", "FR"],
  },
  {
    id: "weekly-review",
    iconBg: "#FDF4FF",
    iconColor: "#9333EA",
    iconType: "chart",
    nameZh: "每周回顾",
    nameEn: "Weekly review",
    descZh: "每周五将近期工作整理成简明的状态更新",
    descEn: "Turn your recent work into a concise status update every Friday",
    promptZh: "回顾我本周完成的工作与代码变更，并起草一份简明扼要的周报状态更新。",
    promptEn: "Review what I worked on this week and draft a short status update.",
    scheduleZh: "星期五 16:00",
    scheduleEn: "Fridays 16:00",
    kind: "cron",
    repeat: "weekly",
    time: "16:00",
    weekdays: ["FR"],
  },
  {
    id: "follow-up-monitor",
    iconBg: "#ECFDF5",
    iconColor: "#059669",
    iconType: "shield",
    nameZh: "跟进监控",
    nameEn: "Follow-up monitor",
    descZh: "检查近期活动状态并标记需要你关注的事项",
    descEn: "Review recent activity and flag anything that needs your attention",
    promptZh: "检查当前活动与任务状态，突出重要变化，并标记任何需要我关注的异常事项。",
    promptEn: "Review recent activity, highlight meaningful changes, and flag anything that needs my attention.",
    scheduleZh: "工作日 09:00",
    scheduleEn: "Weekdays 09:00",
    kind: "heartbeat",
    repeat: "weekdays",
    time: "09:00",
    weekdays: ["MO", "TU", "WE", "TH", "FR"],
  },
];

const WEEKDAYS_LIST: Array<{ code: string; zh: string; en: string }> = [
  { code: "MO", zh: "星期一", en: "Monday" },
  { code: "TU", zh: "星期二", en: "Tuesday" },
  { code: "WE", zh: "星期三", en: "Wednesday" },
  { code: "TH", zh: "星期四", en: "Thursday" },
  { code: "FR", zh: "星期五", en: "Friday" },
  { code: "SA", zh: "星期六", en: "Saturday" },
  { code: "SU", zh: "星期日", en: "Sunday" },
];

export function formatAutomationSchedule(item: Partial<AgentAutomation>, zh: boolean): string {
  if (zh && item.schedule_label_zh) return item.schedule_label_zh;
  if (!zh && item.schedule_label_en) return item.schedule_label_en;
  if (item.schedule_label) return item.schedule_label;
  const t = item.time || "09:00";
  switch (item.repeat) {
    case "weekdays":
      return zh ? `工作日 ${t}` : `Weekdays ${t}`;
    case "weekly": {
      const code = (item.weekdays && item.weekdays[0]) || item.weekday || "FR";
      const found = WEEKDAYS_LIST.find(d => d.code === code);
      return zh ? `${found?.zh || "每周"} ${t}` : `${found?.en || "Weekly"} ${t}`;
    }
    case "hourly":
      return zh ? `每 ${item.interval_hours || 1} 小时` : `Every ${item.interval_hours || 1}h`;
    case "minutely":
      return zh ? `每 ${item.interval_minutes || 15} 分钟` : `Every ${item.interval_minutes || 15}m`;
    case "once":
      return zh ? `一次 (${t})` : `Once (${t})`;
    default:
      return zh ? `每天 ${t}` : `Daily ${t}`;
  }
}

function formatNextRunRelative(nextMs: number | null | undefined, zh: boolean): string {
  if (!nextMs || nextMs <= 0) return "";
  const diffMin = Math.max(1, Math.round((nextMs - Date.now()) / 60_000));
  if (diffMin < 60) {
    return zh ? `下次运行 ${diffMin} 分钟后` : `Next run in ${diffMin}m`;
  }
  const diffHours = Math.round(diffMin / 60);
  if (diffHours < 24) {
    return zh ? `下次运行 ${diffHours} 小时后` : `Next run in ${diffHours}h`;
  }
  const d = new Date(nextMs);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return zh ? `下次运行 ${hh}:${mm}` : `Next run ${hh}:${mm}`;
}

export function HeartbeatWakeupDivider({
  text,
  automationName,
  zh,
}: {
  text: string;
  automationName?: string;
  zh: boolean;
}) {
  const clean = text.replace(/^⏰\s*/, "").trim();
  return (
    <div className="ra-hb-divider" role="separator">
      <span className="ra-hb-divider-text">
        {zh ? "由已安排任务发送" : "Sent by scheduled task"}
        {(automationName || clean) ? ` · ${automationName || clean}` : ""}
      </span>
    </div>
  );
}

/**
 * 1:1 OpenAI Codex `DisplayAutomation` / `AutomationResourceCardView`
 * Minimalist rounded-24px pill card inside chat stream with clock icon, title, schedule, and pencil icon.
 */
export function AutomationChatCard({
  automation,
  zh,
  onEdit,
}: {
  automation: AgentAutomation;
  zh: boolean;
  onEdit: (item: AgentAutomation) => void;
  onRunNow?: (item: AgentAutomation) => void;
}) {
  const paused = automation.status === "PAUSED";
  const scheduleLabel = formatAutomationSchedule(automation, zh);
  const subtitle = paused
    ? `${zh ? "已暂停" : "Paused"} • ${scheduleLabel}`
    : scheduleLabel;

  return (
    <div className={`ra-codex-pill-card${paused ? " is-paused" : ""}`}>
      <button
        type="button"
        className="ra-codex-pill-pressable"
        aria-label={`${automation.name}, ${subtitle}`}
        onClick={() => onEdit(automation)}
      >
        <div className="ra-codex-pill-clock" aria-hidden="true">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor">
            <path d="M12 6.25C12.4142 6.25 12.75 6.5858 12.75 7V11.793C12.75 12.1244 12.6182 12.4424 12.3838 12.6768L10.0303 15.0303C9.7374 15.3232 9.2626 15.3231 8.9697 15.0303C8.6768 14.7374 8.6768 14.2626 8.9697 13.9697L11.25 11.6895V7C11.25 6.5858 11.5858 6.25 12 6.25Z" />
            <path d="M12 2.25C17.3848 2.25 21.75 6.6152 21.75 12C21.75 17.3848 17.3848 21.75 12 21.75C6.6152 21.75 2.25 17.3848 2.25 12C2.25 6.6152 6.6152 2.25 12 2.25ZM12 3.75C7.4437 3.75 3.75 7.4437 3.75 12C3.75 16.5563 7.4437 20.25 12 20.25C16.5563 20.25 20.25 16.5563 20.25 12C20.25 7.4437 16.5563 3.75 12 3.75Z" />
          </svg>
        </div>
        <div className="ra-codex-pill-col">
          <span className="ra-codex-pill-title">
            {automation.name || (zh ? "未命名已安排任务" : "Untitled scheduled task")}
          </span>
          <span className="ra-codex-pill-sub">{subtitle}</span>
        </div>
        <div className="ra-codex-pill-edit" aria-hidden="true">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
            <path d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L6.832 19.82a4.5 4.5 0 01-1.897 1.13l-2.685.8.8-2.685a4.5 4.5 0 011.13-1.897L16.863 4.487zm0 0L19.5 7.125" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </div>
      </button>
    </div>
  );
}

/**
 * Shared Codex Detail / Create / Edit Panel Body (`automation-detail-panel` + `automation-frequency-section`)
 */
function CodexAutomationDetailPane({
  initial,
  runs = [],
  tasks = [],
  activeTaskId,
  zh,
  onClose,
  onSaved,
  onDeleted,
  onRunNow,
  onSelectTask,
}: {
  initial: Partial<AgentAutomation>;
  runs?: AgentAutomationRun[];
  tasks?: AgentTaskSummary[];
  activeTaskId?: string;
  zh: boolean;
  onClose: () => void;
  onSaved: (saved: AgentAutomation) => void;
  onDeleted?: (id: string) => void;
  onRunNow?: (item: AgentAutomation) => void;
  onSelectTask?: (taskId: string) => void;
}) {
  const isEditing = Boolean(initial && initial.id);
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  // Destination ("Runs in" / "运行于"):
  // - "new-chat-each-run" -> kind="cron", target_task_id=""
  // - "new-dedicated-chat" -> kind="heartbeat", target_task_id=""
  // - "thread:<id>" -> kind="heartbeat", target_task_id="<id>"
  const [destination, setDestination] = useState<string>("new-chat-each-run");
  // Repeat mode ("Repeat" / "重复"):
  // "interval" | "daily" | "weekdays" | "weekly" | "once"
  const [repeatMode, setRepeatMode] = useState<"interval" | "daily" | "weekdays" | "weekly" | "once">("daily");
  const [intervalUnit, setIntervalUnit] = useState<"minute" | "hour">("minute");
  const [intervalValue, setIntervalValue] = useState<number>(15);
  const [time, setTime] = useState("09:00");
  const [weekday, setWeekday] = useState("FR");
  const [notificationPolicy, setNotificationPolicy] = useState<"all_runs" | "failed_runs_only">("all_runs");
  const [status, setStatus] = useState<"ACTIVE" | "PAUSED">("ACTIVE");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setName(initial?.name || "");
    setPrompt(initial?.prompt || "");
    const initKind = initial?.kind || (initial?.target_task_id || activeTaskId ? "heartbeat" : "cron");
    const initTarget = initial?.target_task_id || (initKind === "heartbeat" ? activeTaskId || "" : "");
    if (initKind === "cron") {
      setDestination("new-chat-each-run");
    } else if (initTarget) {
      setDestination(`thread:${initTarget}`);
    } else {
      setDestination("new-dedicated-chat");
    }

    const rep = initial?.repeat || "daily";
    if (rep === "minutely") {
      setRepeatMode("interval");
      setIntervalUnit("minute");
      setIntervalValue(initial?.interval_minutes || 15);
    } else if (rep === "hourly") {
      setRepeatMode("interval");
      setIntervalUnit("hour");
      setIntervalValue(initial?.interval_hours || 1);
    } else if (rep === "weekdays" || rep === "weekly" || rep === "once") {
      setRepeatMode(rep);
    } else {
      setRepeatMode("daily");
    }

    setTime(initial?.time || "09:00");
    setWeekday((initial?.weekdays && initial.weekdays[0]) || initial?.weekday || "FR");
    setNotificationPolicy(
      initial?.notification_policy === "failed_runs_only" ? "failed_runs_only" : "all_runs"
    );
    setStatus(initial?.status === "PAUSED" ? "PAUSED" : "ACTIVE");
    setError("");
  }, [initial, activeTaskId]);

  const effectiveKind: "heartbeat" | "cron" =
    destination === "new-chat-each-run" ? "cron" : "heartbeat";
  const effectiveTargetTaskId = destination.startsWith("thread:")
    ? destination.slice("thread:".length)
    : "";

  const availableThreads = useMemo(() => {
    const list: Array<{ id: string; title: string }> = tasks.map(t => ({
      id: t.id,
      title: t.title || t.id,
    }));
    if (
      effectiveTargetTaskId &&
      !list.some(t => t.id === effectiveTargetTaskId)
    ) {
      list.unshift({
        id: effectiveTargetTaskId,
        title: zh ? "当前关联对话" : "Linked chat",
      });
    }
    return list.slice(0, 25);
  }, [tasks, effectiveTargetTaskId, zh]);

  const itemRuns = useMemo(() => {
    if (!initial?.id) return [];
    return runs.filter(r => r.automation_id === initial.id);
  }, [runs, initial?.id]);

  const latestRun = itemRuns[0] || null;

  const initKind = initial?.kind || (initial?.target_task_id || activeTaskId ? "heartbeat" : "cron");
  const initTarget = initial?.target_task_id || (initKind === "heartbeat" ? activeTaskId || "" : "");
  const initNotificationPolicy =
    initial?.notification_policy === "failed_runs_only" ? "failed_runs_only" : "all_runs";
  const initRepeat = initial?.repeat || "daily";
  const initTime = initial?.time || "09:00";
  const initWeekday = (initial?.weekdays && initial.weekdays[0]) || initial?.weekday || "FR";
  const initIntervalHours = initial?.interval_hours || 1;
  const initIntervalMinutes = initial?.interval_minutes || 15;

  const handleSave = async (e?: React.FormEvent, nextStatusOverride?: "ACTIVE" | "PAUSED") => {
    if (e) e.preventDefault();
    const cleanPrompt = prompt.trim();
    if (!cleanPrompt) {
      setError(zh ? "请添加说明" : "Please enter a prompt");
      return;
    }
    const cleanName =
      name.trim() || cleanPrompt.slice(0, 24) || (zh ? "未命名已安排任务" : "Scheduled task");
    const finalStatus = nextStatusOverride || status;
    const finalRepeat: AgentAutomation["repeat"] =
      repeatMode === "interval"
        ? intervalUnit === "minute"
          ? "minutely"
          : "hourly"
        : repeatMode;
    const scheduleChanged =
      !isEditing ||
      finalRepeat !== initRepeat ||
      (repeatMode !== "interval" && time !== initTime) ||
      (finalRepeat === "weekly" && weekday !== initWeekday) ||
      (finalRepeat === "hourly" && intervalValue !== initIntervalHours) ||
      (finalRepeat === "minutely" && intervalValue !== initIntervalMinutes);

    setSaving(true);
    setError("");
    try {
      const payload: Record<string, unknown> = {
        name: cleanName,
        prompt: cleanPrompt,
        status: finalStatus,
        desktop_access: initial?.desktop_access ?? true,
      };
      if (!isEditing || effectiveKind !== initKind || effectiveTargetTaskId !== initTarget) {
        payload.kind = effectiveKind;
        payload.target_task_id = effectiveTargetTaskId;
      }
      if (!isEditing || notificationPolicy !== initNotificationPolicy) {
        payload.notification_policy = notificationPolicy;
      }
      if (scheduleChanged) {
        payload.repeat = finalRepeat;
        if (repeatMode === "interval") {
          payload.interval_hours = intervalUnit === "hour" ? intervalValue : 1;
          payload.interval_minutes = intervalUnit === "minute" ? intervalValue : null;
        } else {
          payload.time = time;
          payload.weekday = weekday;
          if (repeatMode === "weekdays") {
            payload.weekdays = ["MO", "TU", "WE", "TH", "FR"];
          } else if (
            finalRepeat === "weekly" &&
            isEditing &&
            initial?.repeat === "weekly" &&
            weekday === initWeekday &&
            initial.weekdays?.length
          ) {
            payload.weekdays = initial.weekdays;
          }
        }
      }
      const res =
        isEditing && initial?.id
          ? await rynAgent.updateAutomation(initial.id, payload)
          : await rynAgent.createAutomation(payload);
      setStatus(finalStatus);
      onSaved(res.automation);
      if (!isEditing) {
        onClose();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const handleTogglePause = async () => {
    const next = status === "PAUSED" ? "ACTIVE" : "PAUSED";
    if (isEditing && initial?.id) {
      await handleSave(undefined, next);
    } else {
      setStatus(next);
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
    <form className="ra-codex-detail-form" onSubmit={e => void handleSave(e)}>
      <div className="ra-codex-detail-scroll">
        {/* Top Toolbar (`h-toolbar`) */}
        <div className="ra-codex-detail-toolbar">
          <div className="ra-codex-detail-status">
            {!isEditing ? (
              <span className="ra-codex-status-muted">{zh ? "新建" : "New"}</span>
            ) : status === "PAUSED" ? (
              <span className="ra-codex-status-muted">{zh ? "已暂停" : "Paused"}</span>
            ) : (
              <span className="ra-codex-status-active">
                <span className="ra-codex-status-dot" />
                <span>{zh ? "活跃" : "Active"}</span>
              </span>
            )}
          </div>

          <div className="ra-codex-detail-actions">
            {isEditing && initial?.id && onRunNow && (
              <button
                type="button"
                className="ra-codex-toolbar-btn"
                title={zh ? "立即运行" : "Run now"}
                disabled={saving}
                onClick={() => onRunNow(initial as AgentAutomation)}
              >
                <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M8 5v14l11-7z" />
                </svg>
                <span>{zh ? "立即运行" : "Run now"}</span>
              </button>
            )}
            {isEditing && (
              <button
                type="button"
                className="ra-codex-toolbar-icon-btn"
                title={status === "PAUSED" ? (zh ? "继续" : "Resume") : (zh ? "暂停" : "Pause")}
                disabled={saving}
                onClick={() => void handleTogglePause()}
              >
                {status === "PAUSED" ? (
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M8 5v14l11-7z" />
                  </svg>
                ) : (
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M15.75 5.25v13.5m-7.5-13.5v13.5" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                )}
              </button>
            )}
            {isEditing && (
              <button
                type="button"
                className="ra-codex-toolbar-icon-btn is-danger"
                title={zh ? "删除" : "Delete"}
                disabled={saving}
                onClick={() => void handleDelete()}
              >
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9">
                  <path d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
            )}
            <button
              type="button"
              className="ra-codex-toolbar-icon-btn"
              title={zh ? "关闭定时任务详情" : "Close scheduled task details"}
              onClick={onClose}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M6 18L18 6M6 6l12 12" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          </div>
        </div>

        {/* Editable Title (`automation-title-input`) */}
        <input
          type="text"
          data-testid="automation-title-input"
          className="ra-codex-title-input"
          placeholder={zh ? "定时任务标题" : "Scheduled task title"}
          value={name}
          onChange={e => setName(e.target.value)}
          autoFocus={!isEditing}
        />

        {/* Prompt Box */}
        <div className="ra-codex-prompt-card">
          <textarea
            className="ra-codex-prompt-textarea"
            rows={3}
            placeholder={zh ? "描述智能体应该做什么" : "Describe what the agent should do"}
            value={prompt}
            onChange={e => setPrompt(e.target.value)}
          />
        </div>

        {/* Section: 详情 (`inbox.automations.details`) */}
        <section className="ra-codex-section">
          <div className="ra-codex-section-header">{zh ? "详情" : "Details"}</div>
          <div className="ra-codex-kv-group">
            <div className="ra-codex-kv-row">
              <span className="ra-codex-kv-label">{zh ? "运行于" : "Runs in"}</span>
              <select
                className="ra-codex-kv-select"
                aria-label={zh ? "已安排任务的目标对话" : "Scheduled task destination"}
                value={destination}
                onChange={e => setDestination(e.target.value)}
              >
                <option value="new-chat-each-run">
                  {zh ? "每次运行时新建聊天" : "New chat each run"}
                </option>
                <option value="new-dedicated-chat">
                  {zh ? "此任务的新聊天" : "New chat for this task"}
                </option>
                {availableThreads.length > 0 && (
                  <optgroup label={zh ? "现有聊天" : "Existing chats"}>
                    {availableThreads.map(t => (
                      <option key={t.id} value={`thread:${t.id}`}>
                        {t.title || t.id}
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>
            </div>
          </div>
        </section>

        {/* Section: 频率 (`inbox.automations.frequency`) */}
        <section className="ra-codex-section">
          <div className="ra-codex-section-header">{zh ? "频率" : "Frequency"}</div>
          <div className="ra-codex-kv-group">
            {/* Row 1: 重复 */}
            <div className="ra-codex-kv-row">
              <span className="ra-codex-kv-label">{zh ? "重复" : "Repeat"}</span>
              <select
                className="ra-codex-kv-select"
                aria-label={zh ? "重复计划" : "Repeat schedule"}
                value={repeatMode}
                onChange={e =>
                  setRepeatMode(
                    e.target.value as "interval" | "daily" | "weekdays" | "weekly" | "once"
                  )
                }
              >
                <option value="interval">{zh ? "运行频率" : "Interval"}</option>
                <option value="daily">{zh ? "每天" : "Daily"}</option>
                <option value="weekdays">{zh ? "工作日" : "Weekdays"}</option>
                <option value="weekly">{zh ? "每周" : "Weekly"}</option>
                <option value="once">{zh ? "一次" : "Once"}</option>
              </select>
            </div>

            {/* Row 2a: 每隔 (when repeatMode === "interval") */}
            {repeatMode === "interval" && (
              <div className="ra-codex-kv-row">
                <span className="ra-codex-kv-label">{zh ? "每隔" : "Every"}</span>
                <div className="ra-codex-kv-inline">
                  <input
                    type="number"
                    min={1}
                    max={intervalUnit === "minute" ? 720 : 23}
                    className="ra-codex-kv-num"
                    aria-label={zh ? "运行频率数值" : "Interval duration"}
                    value={intervalValue}
                    onChange={e =>
                      setIntervalValue(Math.max(1, Number(e.target.value) || 1))
                    }
                  />
                  <select
                    className="ra-codex-kv-select"
                    aria-label={zh ? "运行频率单位" : "Interval unit"}
                    value={intervalUnit}
                    onChange={e => setIntervalUnit(e.target.value as "minute" | "hour")}
                  >
                    <option value="minute">{zh ? "分钟" : "minutes"}</option>
                    <option value="hour">{zh ? "小时" : "hours"}</option>
                  </select>
                </div>
              </div>
            )}

            {/* Row 2b: 开启 (when repeatMode === "weekly") */}
            {repeatMode === "weekly" && (
              <div className="ra-codex-kv-row">
                <span className="ra-codex-kv-label">{zh ? "开启" : "On"}</span>
                <select
                  className="ra-codex-kv-select"
                  aria-label={zh ? "星期几" : "Day of the week"}
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

            {/* Row 2c: 时间 (when daily / weekdays / weekly / once) */}
            {repeatMode !== "interval" && (
              <div className="ra-codex-kv-row">
                <span className="ra-codex-kv-label">
                  {repeatMode === "once"
                    ? zh
                      ? "开始时间"
                      : "Start time"
                    : zh
                      ? "时间"
                      : "Time"}
                </span>
                <input
                  type="time"
                  className="ra-codex-kv-time"
                  aria-label={zh ? "时间" : "Time"}
                  value={time}
                  onChange={e => setTime(e.target.value || "09:00")}
                />
              </div>
            )}

            {/* Row 3: 通知 (`inbox.automations.notifications.label`) */}
            <div className="ra-codex-kv-row">
              <span className="ra-codex-kv-label">{zh ? "通知" : "Notifications"}</span>
              <select
                className="ra-codex-kv-select"
                aria-label={zh ? "通知" : "Notifications"}
                value={notificationPolicy}
                onChange={e =>
                  setNotificationPolicy(
                    e.target.value as "all_runs" | "failed_runs_only"
                  )
                }
              >
                <option value="all_runs">
                  {effectiveKind === "cron"
                    ? zh
                      ? "所有运行"
                      : "All runs"
                    : zh
                      ? "重要更新"
                      : "Important updates"}
                </option>
                <option value="failed_runs_only">
                  {zh ? "仅限未成功运行" : "Failed runs only"}
                </option>
              </select>
            </div>
          </div>
        </section>

        {error && <div className="ra-codex-error">{error}</div>}
      </div>

      {/* Bottom Footer / Previous Runs (`inbox.automations.history`) */}
      <div className="ra-codex-detail-footer">
        {isEditing && (latestRun || initial.last_run_summary || effectiveTargetTaskId) && (
          <div className="ra-codex-history-box">
            <div className="ra-codex-section-header">
              {zh ? "运行历史记录" : "Previous runs"}
            </div>
            <p className="ra-codex-history-text">
              {latestRun?.inbox_summary ||
                initial.last_run_summary ||
                (zh ? "尚未运行" : "No runs yet")}
            </p>
            {(latestRun?.task_id || initial.last_task_id || effectiveTargetTaskId) &&
              onSelectTask && (
                <button
                  type="button"
                  className="ra-codex-open-chat-btn"
                  onClick={() => {
                    const tid =
                      latestRun?.task_id ||
                      initial.last_task_id ||
                      effectiveTargetTaskId;
                    if (tid) onSelectTask(tid);
                  }}
                >
                  <span>{zh ? "打开聊天" : "Open chat"}</span>
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M4.5 19.5l15-15m0 0H8.25m11.25 0v11.25" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </button>
              )}
          </div>
        )}

        <div className="ra-codex-footer-btns">
          <button
            type="button"
            className="ra-codex-cancel-btn"
            onClick={onClose}
          >
            {zh ? "取消" : "Cancel"}
          </button>
          <button
            type="submit"
            className="ra-codex-primary-btn"
            disabled={saving || !prompt.trim()}
          >
            {saving
              ? zh
                ? "正在保存…"
                : "Saving…"
              : isEditing
                ? zh
                  ? "保存"
                  : "Save"
                : zh
                  ? "创建"
                  : "Create"}
          </button>
        </div>
      </div>
    </form>
  );
}

/**
 * Modal wrapper when opening an automation from inside a chat turn (`AutomationChatCard` pencil click).
 */
export function AutomationDialog({
  open,
  initial,
  zh,
  activeTaskId,
  tasks = [],
  runs = [],
  onClose,
  onSaved,
  onDeleted,
}: {
  open: boolean;
  initial: Partial<AgentAutomation> | null;
  zh: boolean;
  activeTaskId?: string;
  tasks?: AgentTaskSummary[];
  runs?: AgentAutomationRun[];
  onClose: () => void;
  onSaved: (saved: AgentAutomation) => void;
  onDeleted?: (id: string) => void;
}) {
  if (!open) return null;
  return (
    <div className="ra-modal-backdrop" onClick={onClose}>
      <div
        className="ra-codex-dialog-sheet"
        onClick={e => e.stopPropagation()}
      >
        <CodexAutomationDetailPane
          initial={
            initial || {
              kind: activeTaskId ? "heartbeat" : "cron",
              target_task_id: activeTaskId || "",
              repeat: "daily",
              time: "09:00",
              status: "ACTIVE",
            }
          }
          tasks={tasks}
          runs={runs}
          activeTaskId={activeTaskId}
          zh={zh}
          onClose={onClose}
          onSaved={saved => {
            onSaved(saved);
            onClose();
          }}
          onDeleted={onDeleted}
        />
      </div>
    </div>
  );
}

/**
 * 1:1 OpenAI Codex Desktop `AutomationsPage` (`automations-page-f9669b6f7da2.js`) + Stitch Minimalist Split View
 */
export function AutomationsPanel({
  automations,
  runs = [],
  tasks = [],
  activeTaskId,
  zh,
  onRefresh,
  onSelectTask,
}: {
  automations: AgentAutomation[];
  runs?: AgentAutomationRun[];
  tasks?: AgentTaskSummary[];
  activeTaskId?: string;
  zh: boolean;
  onRefresh: () => void;
  onEdit?: (item: Partial<AgentAutomation>) => void;
  onCreateNew?: () => void;
  onSelectTask?: (taskId: string) => void;
}) {
  const [statusFilter, setStatusFilter] = useState<"all" | "active" | "paused">("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedDraft, setSelectedDraft] = useState<Partial<AgentAutomation> | null>(
    () => (automations.length > 0 ? automations[0] : null)
  );
  const [closedManually, setClosedManually] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedDraft && !closedManually && automations.length > 0) {
      setSelectedDraft(automations[0]);
    }
  }, [automations, selectedDraft, closedManually]);

  const threadTitleById = useMemo(() => {
    const map = new Map<string, string>();
    for (const t of tasks) {
      map.set(t.id, t.title || t.id);
    }
    return map;
  }, [tasks]);

  const filteredItems = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return automations.filter(item => {
      const isPaused = item.status === "PAUSED";
      if (statusFilter === "active" && isPaused) return false;
      if (statusFilter === "paused" && !isPaused) return false;
      if (!q) return true;
      const schedule = formatAutomationSchedule(item, zh).toLowerCase();
      return (
        item.name.toLowerCase().includes(q) ||
        item.prompt.toLowerCase().includes(q) ||
        schedule.includes(q)
      );
    });
  }, [automations, statusFilter, searchQuery, zh]);

  const filteredSuggestions = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return CODEX_SUGGESTIONS.filter(s => {
      if (automations.some(a => a.name === s.nameZh || a.name === s.nameEn)) {
        return false;
      }
      if (!q) return true;
      return (
        s.nameZh.toLowerCase().includes(q) ||
        s.nameEn.toLowerCase().includes(q) ||
        s.descZh.toLowerCase().includes(q) ||
        s.descEn.toLowerCase().includes(q)
      );
    });
  }, [automations, searchQuery]);

  const handleToggleStatus = async (item: AgentAutomation, e: React.MouseEvent) => {
    e.stopPropagation();
    setBusyId(item.id);
    try {
      const nextStatus = item.status === "PAUSED" ? "ACTIVE" : "PAUSED";
      const res = await rynAgent.updateAutomation(item.id, { status: nextStatus });
      onRefresh();
      if (selectedDraft?.id === item.id) {
        setSelectedDraft(res.automation);
      }
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

  const openCreatePanel = () => {
    setSelectedDraft({
      kind: activeTaskId ? "heartbeat" : "cron",
      target_task_id: activeTaskId || "",
      repeat: "daily",
      time: "09:00",
      status: "ACTIVE",
    });
  };

  return (
    <div className="ra-codex-split-view">
      {/* 1. Left Main Area (`max-w-[680px]` centered, generous whitespace) */}
      <main className="ra-codex-main-col">
        <div className="ra-codex-main-container">
          {/* Page Header */}
          <header className="ra-codex-page-header">
            <div>
              <h1 className="ra-codex-page-title">
                {zh ? "定时任务" : "Scheduled tasks"}
              </h1>
              <p className="ra-codex-page-subtitle">
                {zh
                  ? "让智能体安排任务、设置提醒或监测更新"
                  : "Ask the agent to schedule tasks, set reminders, or monitor for updates"}
              </p>
            </div>
            <button
              type="button"
              className="ra-codex-create-btn"
              onClick={openCreatePanel}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <path d="M12 4.5v15m7.5-7.5h-15" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              <span>{zh ? "创建" : "Create"}</span>
            </button>
          </header>

          {/* Filter & Search Bar */}
          <section className="ra-codex-filter-bar">
            <div className="ra-codex-search-wrap">
              <svg className="ra-codex-search-icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              <input
                type="text"
                className="ra-codex-search-input"
                placeholder={zh ? "搜索定时任务" : "Search scheduled tasks"}
                value={searchQuery}
                onChange={e => setSearchQuery(e.target.value)}
              />
            </div>

            <div
              className="ra-codex-seg-filter"
              role="group"
              aria-label={zh ? "定时任务状态" : "Scheduled task status"}
            >
              {(
                [
                  { id: "all", zh: "全部", en: "All" },
                  { id: "active", zh: "已开启", en: "Active" },
                  { id: "paused", zh: "已暂停", en: "Paused" },
                ] as const
              ).map(tab => (
                <button
                  key={tab.id}
                  type="button"
                  className={`ra-codex-seg-btn${statusFilter === tab.id ? " is-active" : ""}`}
                  onClick={() => setStatusFilter(tab.id)}
                >
                  {zh ? tab.zh : tab.en}
                </button>
              ))}
            </div>
          </section>

          {/* Scheduled Task List (`automation-row`) */}
          <section className="ra-codex-list" role="list">
            {filteredItems.length === 0 ? (
              <div className="ra-codex-empty">
                {searchQuery.trim()
                  ? zh
                    ? "未找到已安排任务"
                    : "No scheduled tasks found"
                  : statusFilter === "paused"
                    ? zh
                      ? "没有已暂停的任务"
                      : "No paused tasks"
                    : zh
                      ? "暂无定时任务，可点击右上角「创建」或从下方「建议」直接添加"
                      : "No scheduled tasks yet"}
              </div>
            ) : (
              filteredItems.map(item => {
                const isPaused = item.status === "PAUSED";
                const isSelected = selectedDraft?.id === item.id;
                const isHeartbeat = (item.kind || "heartbeat") === "heartbeat";
                const threadLabel = item.target_task_id
                  ? threadTitleById.get(item.target_task_id) ||
                    (zh ? "关联对话" : "Chat")
                  : zh
                    ? "此任务的新聊天"
                    : "Dedicated chat";
                const rowAdornment = isHeartbeat
                  ? zh
                    ? `心跳 • ${threadLabel}`
                    : `Heartbeat • ${threadLabel}`
                  : "";
                const scheduleText = formatAutomationSchedule(item, zh);
                const nextRelative = !isPaused
                  ? formatNextRunRelative(item.next_run_at, zh)
                  : zh
                    ? "已暂停"
                    : "Paused";

                return (
                  <div
                    key={item.id}
                    role="listitem"
                    className={`ra-codex-row${isSelected ? " is-selected" : ""}${isPaused ? " is-paused" : ""}`}
                    onClick={() => setSelectedDraft(item)}
                  >
                    <div className="ra-codex-row-left">
                      <button
                        type="button"
                        className="ra-codex-row-status-icon"
                        disabled={busyId === item.id}
                        title={isPaused ? (zh ? "继续" : "Resume") : (zh ? "暂停" : "Pause")}
                        onClick={e => void handleToggleStatus(item, e)}
                      >
                        {isPaused ? (
                          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
                            <path d="M14.25 9v6m-4.5-6v6m9-3a9 9 0 11-18 0 9 9 0 0118 0z" strokeLinecap="round" strokeLinejoin="round" />
                          </svg>
                        ) : (
                          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
                            <path d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" strokeLinecap="round" strokeLinejoin="round" />
                          </svg>
                        )}
                      </button>

                      <div className="ra-codex-row-body">
                        <div className="ra-codex-row-line1">
                          <span className="ra-codex-row-title">{item.name}</span>
                          {rowAdornment && (
                            <span className="ra-codex-row-adornment">
                              {rowAdornment}
                            </span>
                          )}
                        </div>
                        <div className="ra-codex-row-line2">
                          <span>{scheduleText}</span>
                          {nextRelative && (
                            <>
                              <span className="ra-codex-bullet">•</span>
                              <span>{nextRelative}</span>
                            </>
                          )}
                        </div>
                      </div>
                    </div>

                    <div className="ra-codex-row-right">
                      {!isPaused && <span className="ra-codex-unread-dot" />}
                    </div>
                  </div>
                );
              })
            )}
          </section>

          {/* Suggestions Section (`inbox.automations.suggestions` / `建议`) */}
          {statusFilter !== "paused" && filteredSuggestions.length > 0 && (
            <section className="ra-codex-suggestions">
              <h2 className="ra-codex-suggestions-heading">
                {zh ? "建议" : "Suggestions"}
              </h2>
              <div className="ra-codex-suggestions-list" role="list">
                {filteredSuggestions.map(sug => (
                  <div
                    key={sug.id}
                    role="listitem"
                    className="ra-codex-suggestion-row"
                    onClick={() =>
                      setSelectedDraft({
                        name: zh ? sug.nameZh : sug.nameEn,
                        prompt: zh ? sug.promptZh : sug.promptEn,
                        kind: sug.kind,
                        target_task_id:
                          sug.kind === "heartbeat" ? activeTaskId || "" : "",
                        repeat: sug.repeat,
                        time: sug.time,
                        weekdays: sug.weekdays,
                        status: "ACTIVE",
                      })
                    }
                  >
                    <div className="ra-codex-suggestion-left">
                      <div
                        className="ra-codex-suggestion-icon"
                        style={{
                          backgroundColor: sug.iconBg,
                          color: sug.iconColor,
                        }}
                      >
                        {sug.iconType === "book" && (
                          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <path d="M12 6.042A8.967 8.967 0 006 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 016 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 016-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0018 18a8.967 8.967 0 00-6 2.292m0-14.25v14.25" strokeLinecap="round" strokeLinejoin="round" />
                          </svg>
                        )}
                        {sug.iconType === "chart" && (
                          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <path d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" strokeLinecap="round" strokeLinejoin="round" />
                          </svg>
                        )}
                        {sug.iconType === "shield" && (
                          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <path d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z" strokeLinecap="round" strokeLinejoin="round" />
                          </svg>
                        )}
                      </div>
                      <div className="ra-codex-suggestion-body">
                        <span className="ra-codex-suggestion-title">
                          {zh ? sug.nameZh : sug.nameEn}
                        </span>
                        <span className="ra-codex-suggestion-desc">
                          {zh ? sug.descZh : sug.descEn}
                        </span>
                      </div>
                    </div>
                    <div className="ra-codex-suggestion-schedule">
                      {zh ? sug.scheduleZh : sug.scheduleEn}
                    </div>
                  </div>
                ))}
              </div>
            </section>
          )}
        </div>
      </main>

      {/* 2. Right Detail Panel (`w-[380px] border-l border-[#E5E7EB]`, shown when creating or selecting a task) */}
      {selectedDraft && (
        <aside className="ra-codex-detail-aside">
          <CodexAutomationDetailPane
            initial={selectedDraft}
            runs={runs}
            tasks={tasks}
            activeTaskId={activeTaskId}
            zh={zh}
            onClose={() => {
              setClosedManually(true);
              setSelectedDraft(null);
            }}
            onSaved={saved => {
              onRefresh();
              setSelectedDraft(saved);
            }}
            onDeleted={() => {
              onRefresh();
              setSelectedDraft(null);
            }}
            onRunNow={item => void handleRunNow(item)}
            onSelectTask={onSelectTask}
          />
        </aside>
      )}
    </div>
  );
}
