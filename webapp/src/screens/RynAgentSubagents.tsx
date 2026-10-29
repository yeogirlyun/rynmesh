import { tr } from "../uiI18n";
import React, { useEffect, useMemo, useState } from "react";
import type {
  AgentEvent,
  AgentMessage,
  AgentSubagentSummary,
  AgentSubagentTimelineItem,
} from "../domain/rynAgent";
import ChatMarkdown from "./ChatMarkdown";

const SUBAGENT_TOOL_SET = new Set([
  "ryn_spawn_agent",
  "ryn_send_message",
  "ryn_followup_task",
  "ryn_wait_agent",
  "ryn_interrupt_agent",
  "ryn_close_agent",
  "ryn_list_agents",
  "ryn_resume_agent",
]);

export function isSubagentToolName(name: string): boolean {
  return SUBAGENT_TOOL_SET.has(name);
}

interface PaletteEntry {
  bg: string;
  fg: string;
  accent: string;
  grid: number[][];
}

const AVATAR_PALETTES: Record<string, PaletteEntry> = {
  turing: {
    bg: "#e0f2fe",
    fg: "#0284c7",
    accent: "#38bdf8",
    grid: [
      [0, 1, 1, 1, 0],
      [1, 0, 1, 0, 1],
      [1, 1, 1, 1, 1],
      [0, 1, 0, 1, 0],
      [1, 0, 0, 0, 1],
    ],
  },
  curie: {
    bg: "#f3e8ff",
    fg: "#7c3aed",
    accent: "#a855f7",
    grid: [
      [1, 0, 1, 0, 1],
      [0, 1, 1, 1, 0],
      [1, 1, 0, 1, 1],
      [0, 1, 1, 1, 0],
      [0, 1, 0, 1, 0],
    ],
  },
  feynman: {
    bg: "#fef3c7",
    fg: "#d97706",
    accent: "#f59e0b",
    grid: [
      [0, 1, 0, 1, 0],
      [1, 1, 1, 1, 1],
      [1, 0, 1, 0, 1],
      [0, 1, 1, 1, 0],
      [1, 1, 0, 1, 1],
    ],
  },
  euclid: {
    bg: "#d1fae5",
    fg: "#059669",
    accent: "#10b981",
    grid: [
      [0, 0, 1, 0, 0],
      [0, 1, 1, 1, 0],
      [1, 0, 1, 0, 1],
      [1, 1, 1, 1, 1],
      [0, 1, 0, 1, 0],
    ],
  },
  ada: {
    bg: "#fce7f3",
    fg: "#db2777",
    accent: "#ec4899",
    grid: [
      [0, 1, 1, 1, 0],
      [1, 1, 0, 1, 1],
      [1, 1, 1, 1, 1],
      [0, 1, 1, 1, 0],
      [1, 0, 1, 0, 1],
    ],
  },
  gauss: {
    bg: "#e0e7ff",
    fg: "#4f46e5",
    accent: "#6366f1",
    grid: [
      [1, 1, 0, 1, 1],
      [0, 1, 1, 1, 0],
      [1, 0, 1, 0, 1],
      [0, 1, 1, 1, 0],
      [1, 0, 0, 0, 1],
    ],
  },
  fourier: {
    bg: "#ccfbf1",
    fg: "#0d9488",
    accent: "#14b8a6",
    grid: [
      [0, 1, 0, 1, 0],
      [1, 0, 1, 0, 1],
      [1, 1, 1, 1, 1],
      [0, 1, 0, 1, 0],
      [0, 1, 1, 1, 0],
    ],
  },
};

const FALLBACK_KEYS = Object.keys(AVATAR_PALETTES);

function getPalette(nickname: string): PaletteEntry {
  const key = (nickname || "turing").trim().toLowerCase();
  if (AVATAR_PALETTES[key]) return AVATAR_PALETTES[key];
  let hash = 0;
  for (let i = 0; i < key.length; i++) {
    hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  }
  return AVATAR_PALETTES[FALLBACK_KEYS[hash % FALLBACK_KEYS.length]];
}

export function SubagentPixelAvatar({
  nickname,
  size = 18,
  className = "",
}: {
  nickname: string;
  size?: number;
  className?: string;
}) {
  const palette = getPalette(nickname);
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 7 7"
      className={`ra-subagent-pixel-avatar ${className}`.trim()}
      aria-hidden="true"
    >
      <rect x="0" y="0" width="7" height="7" rx="1.4" fill={palette.bg} />
      {palette.grid.map((row, rIdx) =>
        row.map((cell, cIdx) =>
          cell ? (
            <rect
              key={`${rIdx}-${cIdx}`}
              x={cIdx + 1}
              y={rIdx + 1}
              width="1"
              height="1"
              fill={rIdx === 2 && cIdx === 2 ? palette.accent : palette.fg}
            />
          ) : null,
        ),
      )}
    </svg>
  );
}

function formatDurationClock(seconds: number): string {
  const total = Math.max(0, Math.round(seconds || 0));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function formatDurationShort(seconds: number): string {
  const total = Math.max(1, Math.round(seconds || 0));
  if (total < 60) return `${total}s`;
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return secs > 0 ? `${mins}m ${secs}s` : `${mins}m`;
}

function formatRelativeCompletedTime(completedAtMs?: number | null): string {
  if (!completedAtMs || typeof completedAtMs !== "number") return "刚刚";
  const diffSec = Math.max(0, Math.floor((Date.now() - completedAtMs) / 1000));
  if (diffSec < 60) return "刚刚";
  const mins = Math.floor(diffSec / 60);
  if (mins < 60) return `${mins} 分钟前`;
  const hours = Math.floor(mins / 60);
  return `${hours} 小时前`;
}

function formatChineseNameList(names: string[]): string {
  if (!names.length) return "";
  if (names.length === 1) return names[0];
  if (names.length === 2) return `${names[0]} 和 ${names[1]}`;
  return `${names.slice(0, -1).join("、")} 和 ${names[names.length - 1]}`;
}

function formatEffortLabel(effort?: string): string {
  const e = (effort || "medium").toLowerCase();
  if (e === "low") return "轻度";
  if (e === "high") return "高";
  return "中";
}

function roleLabel(role: string): string {
  const r = (role || "default").toLowerCase();
  if (r === "explorer") return "explorer";
  if (r === "worker") return "worker";
  if (r === "awaiter") return "awaiter";
  return "default";
}

function isSubagentRunning(sub: AgentSubagentSummary): boolean {
  return !sub.closed && (sub.status === "queued" || sub.status === "running");
}

/** Touchpoint 1: Top-right Thread Summary Pill in Header */
export function SubagentHeaderPill({
  subagents,
  isOpen,
  onToggle,
}: {
  subagents: AgentSubagentSummary[];
  isOpen: boolean;
  onToggle: () => void;
}) {
  if (!subagents.length) return null;
  const activeCount = subagents.filter(
    s => !s.closed && (s.status === "queued" || s.status === "running" || s.is_waiting),
  ).length;
  const completedCount = subagents.filter(s => s.status === "completed").length;
  const previewAgents = subagents.slice(0, 3);

  return (
    <button
      type="button"
      className={`ra-subagent-header-pill${isOpen ? " is-open" : ""}${activeCount > 0 ? " is-active" : ""}`}
      onClick={onToggle}
      title={tr("查看并管理当前会话的子智能体 (Subagents)")}
    >
      <span className="ra-subagent-avatar-stack">
        {previewAgents.map(agent => (
          <span key={agent.id} className="ra-subagent-avatar-stack-item">
            <SubagentPixelAvatar nickname={agent.nickname} size={16} />
          </span>
        ))}
      </span>
      <span className="ra-subagent-header-pill-label"> {tr("子智能体")} {activeCount > 0 ? (
          <>
            {" · "}
            <strong className="is-running">{activeCount} {tr("个运行中")}</strong>
          </>
        ) : null}
        {completedCount > 0 ? (
          <>
            {" · "}
            <span>{completedCount} {tr("完成")}</span>
          </>
        ) : null}
        {activeCount === 0 && completedCount === 0 ? (
          <>
            {" · "}
            <span>{subagents.length} {tr("个")}</span>
          </>
        ) : null}
      </span>
      <span className="ra-subagent-header-pill-chevron" aria-hidden="true">▾</span>
    </button>
  );
}

export interface SubagentCallItem {
  callId: string;
  toolName: string;
  args: Record<string, unknown>;
  result: Record<string, unknown> | null;
  matchedSubagent: AgentSubagentSummary | null;
  matchedSubagents: AgentSubagentSummary[];
}

export function extractSubagentCallsFromMessages(
  messages: AgentMessage[],
  events: AgentEvent[],
  subagents: AgentSubagentSummary[],
): SubagentCallItem[] {
  return extractSubagentCallBatchesFromMessages(messages, events, subagents).flat();
}

export function extractSubagentCallBatchesFromMessages(
  messages: AgentMessage[],
  events: AgentEvent[],
  subagents: AgentSubagentSummary[],
): SubagentCallItem[][] {
  const resultByCallId = new Map<string, Record<string, unknown>>();
  for (const ev of events) {
    if (ev.type === "tool_result" && typeof ev.call_id === "string" && ev.result && typeof ev.result === "object") {
      resultByCallId.set(ev.call_id, ev.result as Record<string, unknown>);
    }
  }
  const byId = new Map<string, AgentSubagentSummary>();
  const byNick = new Map<string, AgentSubagentSummary>();
  const byTaskName = new Map<string, AgentSubagentSummary>();
  const byPath = new Map<string, AgentSubagentSummary>();
  for (const sub of subagents) {
    byId.set(sub.id, sub);
    byId.set(sub.id.toLowerCase(), sub);
    byNick.set(sub.nickname.toLowerCase(), sub);
    byTaskName.set(sub.task_name.toLowerCase(), sub);
    byPath.set(sub.agent_path.toLowerCase(), sub);
  }

  const resolveToken = (raw: unknown): AgentSubagentSummary | null => {
    if (typeof raw !== "string" || !raw.trim()) return null;
    const key = raw.trim().replace(/^@/, "").toLowerCase();
    return byId.get(raw.trim()) || byId.get(key) || byNick.get(key) || byTaskName.get(key) || byPath.get(key) || null;
  };

  const batches: SubagentCallItem[][] = [];
  let currentBatch: SubagentCallItem[] = [];
  let seenWaitInCurrentBatch = false;

  for (const msg of messages) {
    if (msg.role !== "assistant" || !msg.tool_calls?.length) continue;
    for (const tc of msg.tool_calls) {
      const tname = tc.function?.name || "";
      if (!isSubagentToolName(tname)) continue;
      let parsedArgs: Record<string, unknown> = {};
      try {
        parsedArgs = JSON.parse(tc.function.arguments || "{}");
      } catch {
        parsedArgs = {};
      }
      const res = resultByCallId.get(tc.id) || null;
      const matchedList: AgentSubagentSummary[] = [];
      const addMatched = (candidate: AgentSubagentSummary | null | undefined) => {
        if (candidate && !matchedList.some(m => m.id === candidate.id)) {
          matchedList.push(candidate);
        }
      };

      addMatched(resolveToken(res?.agent_id));
      addMatched(resolveToken(res?.nickname));
      addMatched(resolveToken(parsedArgs.target));
      addMatched(resolveToken(parsedArgs.task_name));

      if (Array.isArray(parsedArgs.targets)) {
        for (const t of parsedArgs.targets) {
          if (typeof t === "string" && (t.toLowerCase() === "all" || t === "*")) {
            for (const s of subagents) addMatched(s);
          } else {
            addMatched(resolveToken(t));
          }
        }
      }
      for (const arrKey of ["agents", "interrupted", "closed"] as const) {
        const listVal = res?.[arrKey];
        if (Array.isArray(listVal)) {
          for (const entry of listVal) {
            if (entry && typeof entry === "object") {
              const rec = entry as Record<string, unknown>;
              addMatched(resolveToken(rec.agent_id) || resolveToken(rec.nickname) || resolveToken(rec.agent_path));
            }
          }
        }
      }

      if (tname === "ryn_spawn_agent" && seenWaitInCurrentBatch && currentBatch.length > 0) {
        batches.push(currentBatch);
        currentBatch = [];
        seenWaitInCurrentBatch = false;
      }
      if (tname === "ryn_wait_agent") {
        seenWaitInCurrentBatch = true;
      }
      currentBatch.push({
        callId: tc.id,
        toolName: tname,
        args: parsedArgs,
        result: res,
        matchedSubagent: matchedList[0] || null,
        matchedSubagents: matchedList,
      });
    }
  }
  if (currentBatch.length > 0) {
    batches.push(currentBatch);
  }
  return batches;
}

/** Touchpoint 2: Main Chat Inline Subagent Action Group */
export function SubagentInlineGroup({
  calls,
  subagents: _subagents,
  onSelectSubagent,
}: {
  calls: SubagentCallItem[];
  subagents: AgentSubagentSummary[];
  onSelectSubagent?: (subagentId: string) => void;
}) {
  const [groupOpen, setGroupOpen] = useState(true);
  const [expandedCalls, setExpandedCalls] = useState<Record<string, boolean>>({});

  if (!calls.length) return null;

  const spawnCalls = calls.filter(c => c.toolName === "ryn_spawn_agent");
  const waitCalls = calls.filter(c => c.toolName === "ryn_wait_agent");
  const otherCalls = calls.filter(c => c.toolName !== "ryn_spawn_agent" && c.toolName !== "ryn_wait_agent");
  const msgCalls = otherCalls.filter(c => c.toolName === "ryn_send_message" || c.toolName === "ryn_followup_task");

  const headerTitle =
    spawnCalls.length > 0
      ? `已创建 ${spawnCalls.length} 个智能体`
      : msgCalls.length === otherCalls.length && msgCalls.length > 0
      ? `已向子智能体发送 ${msgCalls.length} 条指令`
      : otherCalls.length > 0
      ? `已执行 ${otherCalls.length} 项子智能体操作`
      : `正在等待子智能体汇总 (${waitCalls.length})`;

  const involvedAgents: AgentSubagentSummary[] = [];
  const seenIds = new Set<string>();
  for (const c of calls) {
    for (const m of c.matchedSubagents) {
      if (!seenIds.has(m.id)) {
        seenIds.add(m.id);
        involvedAgents.push(m);
      }
    }
  }

  const allSettled = involvedAgents.length > 0 && involvedAgents.every(a => !isSubagentRunning(a) && !a.is_waiting);
  const namesList = involvedAgents.map(a => a.nickname);
  const formattedNames = formatChineseNameList(namesList);

  return (
    <div className="ra-subagent-inline-group">
      <button
        type="button"
        className="ra-subagent-inline-header"
        onClick={() => setGroupOpen(prev => !prev)}
      >
        <span className="ra-subagent-inline-chevron">{groupOpen ? "▼" : "▶"}</span>
        <span className="ra-subagent-inline-title">{headerTitle}</span>
        {involvedAgents.length > 0 && (
          <span className="ra-subagent-inline-meta">
            {involvedAgents.filter(isSubagentRunning).length > 0
              ? `${involvedAgents.filter(isSubagentRunning).length} 个运行中`
              : `${involvedAgents.filter(a => a.status === "completed").length}/${involvedAgents.length} 已完成`}
          </span>
        )}
      </button>

      {groupOpen && (
        <div className="ra-subagent-inline-rows">
          {calls
            .filter(c => c.toolName !== "ryn_wait_agent" || calls.length === waitCalls.length)
            .map(item => {
              const sub = item.matchedSubagent;
              const nickname =
                sub?.nickname ||
                (typeof item.result?.nickname === "string" ? item.result.nickname : null) ||
                (typeof item.args.task_name === "string" ? item.args.task_name : "Turing");
              const role =
                sub?.role ||
                (typeof item.result?.role === "string" ? item.result.role : null) ||
                (typeof item.args.role === "string" ? item.args.role : "worker");
              const instructions =
                sub?.instructions ||
                (typeof item.args.prompt === "string" ? item.args.prompt : "") ||
                (typeof item.args.message === "string" ? item.args.message : "");
              const hasInstructions = Boolean(instructions);
              const isExpanded = Boolean(expandedCalls[item.callId]);

              const renderActionLabel = () => {
                if (item.toolName === "ryn_spawn_agent") {
                  return (
                    <>
                      <span className="ra-subagent-inline-action-verb">{tr("已使用以下指令创建")} </span>
                      <strong className="ra-subagent-inline-nick">{nickname}</strong>
                      <span className="ra-subagent-inline-role-tag">({roleLabel(role)})：</span>
                    </>
                  );
                }
                if (item.toolName === "ryn_send_message") {
                  return (
                    <>
                      <span className="ra-subagent-inline-action-verb">{tr("已向")} </span>
                      <strong className="ra-subagent-inline-nick">{nickname}</strong>
                      <span className="ra-subagent-inline-action-verb"> {tr("发送补充消息：")}</span>
                    </>
                  );
                }
                if (item.toolName === "ryn_followup_task") {
                  return (
                    <>
                      <span className="ra-subagent-inline-action-verb">{tr("已向")} </span>
                      <strong className="ra-subagent-inline-nick">{nickname}</strong>
                      <span className="ra-subagent-inline-action-verb"> {tr("委派后续任务：")}</span>
                    </>
                  );
                }
                if (item.toolName === "ryn_interrupt_agent") {
                  return (
                    <>
                      <span className="ra-subagent-inline-action-verb">{tr("已中断子智能体")} </span>
                      <strong className="ra-subagent-inline-nick">{nickname}</strong>
                    </>
                  );
                }
                if (item.toolName === "ryn_close_agent") {
                  return (
                    <>
                      <span className="ra-subagent-inline-action-verb">{tr("已关闭子智能体")} </span>
                      <strong className="ra-subagent-inline-nick">{nickname}</strong>
                    </>
                  );
                }
                if (item.toolName === "ryn_resume_agent") {
                  return (
                    <>
                      <span className="ra-subagent-inline-action-verb">{tr("已恢复子智能体")} </span>
                      <strong className="ra-subagent-inline-nick">{nickname}</strong>
                    </>
                  );
                }
                if (item.toolName === "ryn_list_agents") {
                  return <span className="ra-subagent-inline-action-verb">{tr("已列出当前线程树的子智能体")}</span>;
                }
                return <span className="ra-subagent-inline-action-verb">{tr("等待子智能体完成并收集 &lt;subagent_notification&gt;")}</span>;
              };

              const running = sub ? isSubagentRunning(sub) : false;
              const waiting = sub ? sub.is_waiting : false;
              const completed = sub ? sub.status === "completed" : Boolean(item.result?.ok);
              const hasDiff = Boolean(sub && (sub.diff_stats?.added > 0 || sub.diff_stats?.removed > 0));

              return (
                <div key={item.callId} className="ra-subagent-inline-row-wrap">
                  <div className="ra-subagent-inline-row">
                    <button
                      type="button"
                      className="ra-subagent-inline-row-toggle"
                      onClick={() =>
                        hasInstructions && setExpandedCalls(prev => ({ ...prev, [item.callId]: !prev[item.callId] }))
                      }
                    >
                      {hasInstructions ? (
                        <span className="ra-subagent-inline-chevron-sm">{isExpanded ? "▼" : "›"}</span>
                      ) : (
                        <span className="ra-subagent-inline-chevron-sm" />
                      )}
                      <SubagentPixelAvatar nickname={nickname} size={16} />
                      <span className="ra-subagent-inline-row-text">
                        {renderActionLabel()}
                        {instructions ? <span className="ra-subagent-inline-prompt-snippet">{instructions}</span> : null}
                      </span>
                    </button>

                    <button
                      type="button"
                      className={`ra-subagent-status-pill ${
                        running ? "is-running" : waiting ? "is-waiting" : completed ? "is-completed" : "is-muted"
                      }`}
                      onClick={() => sub && onSelectSubagent?.(sub.id)}
                      title={sub ? `点击在右侧面板查看 ${nickname} 的实时轨迹与代码变更` : undefined}
                    >
                      {running ? (
                        <>
                          <span className="ra-subagent-dot is-running" />
                          <span> {tr("正在运行 ·")} {sub?.current_step_text || "执行中..."}
                            {hasDiff && sub ? ` (+${sub.diff_stats.added} -${sub.diff_stats.removed})` : ""}
                          </span>
                        </>
                      ) : waiting ? (
                        <>
                          <span className="ra-subagent-dot is-waiting" />
                          <span>{tr("正在等待指示 ·")} {sub?.current_step_text || "等待中"}</span>
                        </>
                      ) : completed ? (
                        <>
                          <span className="ra-subagent-dot is-completed" />
                          <span>{tr("已完成 (")}{formatDurationShort(sub?.elapsed_seconds || 1)})</span>
                        </>
                      ) : (
                        <span>
                          {sub?.status === "paused"
                            ? "已中断"
                            : sub?.status === "failed"
                            ? "执行失败"
                            : sub?.status === "cancelled"
                            ? "已取消"
                            : sub?.status || "已调度"}
                        </span>
                      )}
                    </button>
                  </div>

                  {isExpanded && instructions && (
                    <div className="ra-subagent-inline-expanded">
                      <div className="ra-subagent-inline-expanded-meta">
                        <span>{tr("Agent Path:")} <code>{sub?.agent_path || `/root/${nickname.toLowerCase()}`}</code></span>
                        {sub?.write_scope?.length ? (
                          <span> {tr("负责范围:")} {sub.write_scope.map(s => <code key={s} className="ra-subagent-scope-code">{s}</code>)}
                          </span>
                        ) : null}
                      </div>
                      <div className="ra-subagent-inline-expanded-prompt">{instructions}</div>
                    </div>
                  )}
                </div>
              );
            })}
        </div>
      )}

      {involvedAgents.length > 0 && (
        <div
          className="ra-subagent-inline-summary-footer"
          onClick={() => involvedAgents[0] && onSelectSubagent?.(involvedAgents[0].id)}
          role="button"
          tabIndex={0}
          onKeyDown={e => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              if (involvedAgents[0]) onSelectSubagent?.(involvedAgents[0].id);
            }
          }}
        >
          <span className="ra-subagent-avatar-stack">
            {involvedAgents.slice(0, 4).map(agent => (
              <span key={agent.id} className="ra-subagent-avatar-stack-item">
                <SubagentPixelAvatar nickname={agent.nickname} size={16} />
              </span>
            ))}
          </span>
          <span className="ra-subagent-inline-summary-text">
            {allSettled ? `${formattedNames} 已完成` : `${formattedNames} 开始工作`}
          </span>
          <span className="ra-subagent-inline-summary-link">{tr("查看详情 →")}</span>
        </div>
      )}
    </div>
  );
}

/** Touchpoint 3: Composer Background Subagents Bar */
export function SubagentComposerBar({
  subagents,
  onSelectSubagent,
  onMentionSubagent,
  onStopAll,
}: {
  subagents: AgentSubagentSummary[];
  onSelectSubagent: (subagentId: string) => void;
  onMentionSubagent: (nickname: string) => void;
  onStopAll: () => void;
}) {
  const [expanded, setExpanded] = useState(true);
  if (!subagents.length) return null;

  const activeSubagents = subagents.filter(
    s => !s.closed && (s.status === "queued" || s.status === "running" || s.is_waiting),
  );
  const nonClosed = subagents.filter(s => !s.closed);
  const displayList = activeSubagents.length > 0 ? activeSubagents : nonClosed.length > 0 ? nonClosed : subagents;
  const stoppableCount = subagents.filter(s => !s.closed && (isSubagentRunning(s) || s.is_waiting)).length;

  const totalDiff = useMemo(() => {
    return subagents.reduce(
      (acc, s) => ({
        added: acc.added + (s.diff_stats?.added || 0),
        removed: acc.removed + (s.diff_stats?.removed || 0),
      }),
      { added: 0, removed: 0 },
    );
  }, [subagents]);

  return (
    <div className="ra-subagent-composer-bar">
      <div className="ra-subagent-composer-bar-header">
        <button
          type="button"
          className="ra-subagent-composer-bar-toggle"
          onClick={() => setExpanded(prev => !prev)}
        >
          <span className="ra-subagent-inline-chevron">{expanded ? "▼" : "▶"}</span>
          <span className="ra-subagent-composer-bar-title">
            {displayList.length} {tr("个后台智能体")} </span>
          <span className="ra-subagent-composer-bar-hint">{tr("（使用 @ 标记智能体）")}</span>
        </button>

        <div className="ra-subagent-composer-bar-actions">
          {(totalDiff.added > 0 || totalDiff.removed > 0) && (
            <span className="ra-subagent-diff-stat">
              <span className="is-add">+{totalDiff.added}</span>
              <span className="is-del">-{totalDiff.removed}</span>
            </span>
          )}
          {stoppableCount > 0 && (
            <button
              type="button"
              className="ra-subagent-stop-all-btn"
              onClick={onStopAll}
              title={tr("停止所有正在运行的子智能体")}
            > {tr("■ 全部停止")} </button>
          )}
        </div>
      </div>

      {expanded && (
        <div className="ra-subagent-composer-bar-list">
          {displayList.map(sub => {
            const running = isSubagentRunning(sub);
            const waiting = sub.is_waiting;
            const completed = sub.status === "completed";
            const added = sub.diff_stats?.added || 0;
            const removed = sub.diff_stats?.removed || 0;
            const zeroDiff = added === 0 && removed === 0;
            return (
              <div key={sub.id} className="ra-subagent-composer-row">
                <div
                  className="ra-subagent-composer-row-left"
                  onClick={() => onSelectSubagent(sub.id)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={e => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onSelectSubagent(sub.id);
                    }
                  }}
                >
                  <SubagentPixelAvatar nickname={sub.nickname} size={16} />
                  <span className="ra-subagent-composer-nick">{sub.nickname}</span>
                  <span className={`ra-subagent-role-badge is-${roleLabel(sub.role)}`}>
                    {roleLabel(sub.role).toUpperCase()}
                  </span>
                  <span
                    className={`ra-subagent-composer-status ${
                      running ? "is-running" : waiting ? "is-waiting" : completed ? "is-completed" : ""
                    }`}
                  >
                    {running
                      ? `正在运行 · ${sub.current_step_text || "执行中"}`
                      : waiting
                      ? `正在等待指示 · ${sub.current_step_text || "等待中"}`
                      : completed
                      ? `已完成 · ${sub.conclusion_preview || sub.current_step_text || "任务完成"}`
                      : sub.current_step_text || sub.status}
                  </span>
                </div>

                <div className="ra-subagent-composer-row-right">
                  <button
                    type="button"
                    className="ra-subagent-mention-chip"
                    onClick={() => onMentionSubagent(sub.nickname)}
                    title={`在输入框中标记 @${sub.nickname}`}
                  >
                    @{sub.nickname}
                  </button>
                  {sub.model_id && (
                    <span className="ra-subagent-composer-model"> {tr("使用")} {sub.model_id.split("/").pop()}
                    </span>
                  )}
                  <span className={`ra-subagent-diff-stat${zeroDiff ? " is-zero" : ""}`}>
                    <span className={zeroDiff ? "is-zero" : "is-add"}>+{added}</span>
                    <span className={zeroDiff ? "is-zero" : "is-del"}>-{removed}</span>
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** Touchpoint 4: Right-Side Subagents Drawer (Overview List + Drilldown Detail View) */
export function SubagentRightDrawer({
  subagents,
  selectedSubagentId,
  onSelectSubagent,
  onClose,
  onInterruptSubagent,
  onMentionSubagent,
}: {
  subagents: AgentSubagentSummary[];
  selectedSubagentId: string | null;
  onSelectSubagent: (id: string | null) => void;
  onClose: () => void;
  onInterruptSubagent: (subagentId: string) => void;
  onMentionSubagent: (nickname: string, quoteText?: string) => void;
}) {
  const [expandedReadGroups, setExpandedReadGroups] = useState<Record<number, boolean>>({});
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [liveTickSec, setLiveTickSec] = useState(0);

  const hasRunning = useMemo(() => subagents.some(isSubagentRunning), [subagents]);

  useEffect(() => {
    setLiveTickSec(0);
  }, [subagents]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setLiveTickSec(prev => (hasRunning ? Math.min(4, prev + 1) : prev));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [hasRunning]);

  const selected = useMemo(
    () => (selectedSubagentId ? subagents.find(s => s.id === selectedSubagentId) || null : null),
    [subagents, selectedSubagentId],
  );

  const activeList = useMemo(
    () =>
      subagents.filter(
        s => !s.closed && (s.status === "queued" || s.status === "running" || s.is_waiting),
      ),
    [subagents],
  );
  const completedList = useMemo(
    () =>
      subagents.filter(
        s => s.closed || (s.status !== "queued" && s.status !== "running" && !s.is_waiting),
      ),
    [subagents],
  );
  const waitingCount = useMemo(
    () => activeList.filter(s => s.is_waiting).length,
    [activeList],
  );
  const avgCompletedSec = useMemo(() => {
    if (!completedList.length) return 0;
    const sum = completedList.reduce((acc, s) => acc + (s.elapsed_seconds || 0), 0);
    return Math.round(sum / completedList.length);
  }, [completedList]);

  if (selected) {
    const running = isSubagentRunning(selected);
    const waiting = selected.is_waiting;
    const completed = selected.status === "completed";
    const isExplorer = selected.role === "explorer";
    const stepIdx = selected.step_index || Math.max(1, selected.steps || 1);
    const estTotal = selected.estimated_total_steps || Math.max(stepIdx, 4);
    const liveElapsed = (selected.elapsed_seconds || 0) + (running ? liveTickSec : 0);

    return (
      <aside className="ra-subagent-drawer" aria-label={tr("子智能体详情面板")}>
        {/* Compact Stitch Top Navigation & Identity Bar */}
        <div className="ra-subagent-drilldown-identity">
          <div className="ra-subagent-drilldown-identity-left">
            <button
              type="button"
              className="ra-subagent-back-btn"
              onClick={() => onSelectSubagent(null)}
              title={tr("返回子代理总览列表")}
            > {tr("← 子代理")} </button>
            <span className="ra-subagent-drilldown-sep" aria-hidden="true">/</span>
            <SubagentPixelAvatar nickname={selected.nickname} size={22} />
            <div className="ra-subagent-drilldown-titles">
              <div className="ra-subagent-drilldown-name-row">
                <span className="ra-subagent-drilldown-nick">{selected.nickname}</span>
                <span className={`ra-subagent-role-badge is-${roleLabel(selected.role)}`}>
                  {isExplorer ? "explorer · 只读" : roleLabel(selected.role)}
                </span>
                <span className="ra-subagent-drilldown-model">
                  {selected.model_id ? selected.model_id.split("/").pop() : "default"} · {formatEffortLabel(selected.reasoning_effort)}
                </span>
              </div>
              <div className="ra-subagent-drilldown-path">{selected.agent_path}</div>
            </div>
          </div>

          <div className="ra-subagent-drilldown-identity-right">
            <div
              className={`ra-subagent-drilldown-badge ${
                running ? "is-running" : waiting ? "is-waiting" : completed ? "is-completed" : ""
              }`}
            >
              {running
                ? `● ${formatDurationClock(liveElapsed)}`
                : waiting
                ? "● 等待指示"
                : completed
                ? `✓ 已完成 · ${formatRelativeCompletedTime(selected.completed_at_ms)}`
                : selected.status === "paused"
                ? "⏸ 已中断"
                : selected.status === "failed"
                ? "✕ 执行失败"
                : selected.status === "cancelled"
                ? "⊘ 已取消"
                : selected.status}
            </div>
            <button
              type="button"
              className="ra-subagent-close-btn"
              onClick={onClose}
              aria-label={tr("关闭子智能体面板")}
            >
              ×
            </button>
          </div>
        </div>

        <div className="ra-subagent-drawer-body">
          {/* Section 1: Delegated Task & Write Scope */}
          <div className="ra-subagent-detail-section">
            <div className="ra-subagent-detail-section-header">
              <span>
                {isExplorer
                  ? `委派的任务 (${selected.agent_path})`
                  : "委派的任务 (由 /root 主智能体派发 · 独立上下文)"}
              </span>
              <code className="ra-subagent-task-id">
                {isExplorer ? "只读排查沙盒" : `task_id: #${selected.id.slice(-4)}`}
              </code>
            </div>
            <div className="ra-subagent-scope-row">
              {isExplorer ? (
                <span className="ra-subagent-scope-pill is-readonly"> {tr("READ-ONLY · 只读探索沙盒")} </span>
              ) : selected.write_scope?.length ? (
                selected.write_scope.map(scopePath => (
                  <span key={scopePath} className="ra-subagent-scope-pill"> {tr("&lt;&gt; 负责：")}{scopePath}
                  </span>
                ))
              ) : (
                <span className="ra-subagent-scope-pill">{tr("WORKSPACE · 工作区写入权限")}</span>
              )}
            </div>
            <div className="ra-subagent-instructions-box">{selected.instructions}</div>
          </div>

          {/* Section 2: Real-time Execution Timeline & Inline Unified Diff */}
          <div className="ra-subagent-detail-section">
            <div className="ra-subagent-detail-section-header">
              <span>{tr("内部执行轨迹 (Real-time Timeline)")}</span>
              <div className="ra-subagent-timeline-header-right">
                <span className="ra-subagent-timeline-step-badge">
                  {running
                    ? `步骤 ${stepIdx}/${estTotal} 正在处理`
                    : `共 ${Math.max(1, selected.timeline.length)} 步已完成`}
                </span>
                {(selected.diff_stats?.added > 0 || selected.diff_stats?.removed > 0) && (
                  <span className="ra-subagent-diff-stat">
                    <span className="is-add">+{selected.diff_stats.added}</span>
                    <span className="is-del">-{selected.diff_stats.removed}</span>
                  </span>
                )}
              </div>
            </div>

            <div className="ra-subagent-timeline">
              {selected.timeline.length === 0 ? (
                <div className="ra-subagent-timeline-empty">
                  {running ? "正在初始化上下文并执行第一步..." : "暂无工具调用记录"}
                </div>
              ) : (
                selected.timeline.map((item: AgentSubagentTimelineItem, idx: number) => {
                  if (item.kind === "read_group") {
                    const isOpen = expandedReadGroups[idx] ?? isExplorer;
                    return (
                      <div key={idx} className="ra-subagent-timeline-node">
                        <span className="ra-subagent-node-check" aria-hidden="true">✓</span>
                        <div className="ra-subagent-timeline-read-group">
                          <button
                            type="button"
                            className="ra-subagent-timeline-read-toggle"
                            onClick={() =>
                              setExpandedReadGroups(prev => ({ ...prev, [idx]: !isOpen }))
                            }
                          >
                            <span>{isOpen ? "▼" : "▶"}</span>
                            <span className="ra-subagent-read-toggle-title"> {tr("用时")} {formatDurationShort(selected.elapsed_seconds)} {tr("· 执行了")} {item.count} {tr("次只读搜索与文件读取")} </span>
                            <span className="ra-subagent-read-toggle-action">
                              {isOpen ? "点击收起" : "点击展开"}
                            </span>
                          </button>
                          {isOpen && (
                            <div className="ra-subagent-timeline-read-items">
                              {item.items.map((rStep, rIdx) => {
                                const prefix =
                                  rStep.tool === "ryn_file_search"
                                    ? "grep:"
                                    : rStep.tool === "ryn_file_read"
                                    ? "read:"
                                    : "list:";
                                return (
                                  <div key={rIdx} className="ra-subagent-timeline-read-item">
                                    <span className="ra-subagent-read-bullet">●</span>
                                    <span className="ra-subagent-read-prefix">{prefix}</span>
                                    <span className="ra-subagent-read-text">
                                      {rStep.target}
                                      {rStep.line_range ? ` (${rStep.line_range})` : ""}
                                    </span>
                                    <span className="ra-subagent-step-dur">
                                      {rStep.duration_seconds || 1}{tr("s")} </span>
                                  </div>
                                );
                              })}
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  }

                  if (item.kind === "file_edit") {
                    return (
                      <div key={idx} className="ra-subagent-timeline-node">
                        <span className="ra-subagent-node-check" aria-hidden="true">✓</span>
                        <div className="ra-subagent-timeline-diff-card">
                          <div className="ra-subagent-timeline-diff-header">
                            <span className="ra-subagent-diff-path">{tr("已编辑")} {item.path}</span>
                            <div className="ra-subagent-diff-header-right">
                              <span className="ra-subagent-diff-stat">
                                <span className="is-add">+{item.added}</span>
                                <span className="is-del">-{item.removed}</span>
                              </span>
                              <span className="ra-subagent-step-dur">
                                {item.duration_seconds || 2}{tr("s")} </span>
                            </div>
                          </div>
                          {item.diff_lines?.length > 0 && (
                            <div className="ra-subagent-unified-diff">
                              {item.diff_lines.map((dLine, lIdx) => (
                                <div
                                  key={lIdx}
                                  className={`ra-subagent-diff-line is-${dLine.type}`}
                                >
                                  <span className="ra-subagent-diff-ln">
                                    {dLine.old_lineno ?? ""}
                                  </span>
                                  <span className="ra-subagent-diff-ln">
                                    {dLine.new_lineno ?? ""}
                                  </span>
                                  <span className="ra-subagent-diff-sign">
                                    {dLine.type === "add" ? "+" : dLine.type === "del" ? "-" : " "}
                                  </span>
                                  <span className="ra-subagent-diff-code">{dLine.text}</span>
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  }

                  if (item.kind === "thought") {
                    return (
                      <div key={idx} className="ra-subagent-timeline-node">
                        <span className="ra-subagent-node-check" aria-hidden="true">✓</span>
                        <div className="ra-subagent-timeline-thought">
                          <span className="ra-subagent-thought-icon">◈</span>
                          <span>{item.text}</span>
                        </div>
                      </div>
                    );
                  }

                  return (
                    <div key={idx} className="ra-subagent-timeline-node">
                      <span className="ra-subagent-node-check" aria-hidden="true">✓</span>
                      <div className="ra-subagent-timeline-tool">
                        <span className="ra-subagent-tool-bullet">⚙</span>
                        <span className="ra-subagent-tool-text">{item.summary}</span>
                        <span className="ra-subagent-step-dur">{item.duration_seconds || 1}{tr("s")}</span>
                      </div>
                    </div>
                  );
                })
              )}

              {running && (
                <div className="ra-subagent-timeline-node is-active">
                  <span className="ra-subagent-node-pulse" aria-hidden="true">
                    <span className="ra-subagent-dot is-running" />
                  </span>
                  <div className="ra-subagent-timeline-live">
                    <div className="ra-subagent-timeline-live-top">
                      <span>{selected.current_step_text || "正在执行中..."}</span>
                      <span className="ra-subagent-live-pill">{tr("进行中")}</span>
                    </div>
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Section 3: Final Reply & <subagent_notification> */}
          {(selected.final_reply || selected.notification_payload) && (
            <div className="ra-subagent-detail-section is-final-reply">
              <div className="ra-subagent-detail-section-header">
                <span>{tr("✓ 最终回复 · 已自动注入主智能体上下文")}</span>
                <code>{tr("&lt;subagent_notification&gt;")}</code>
              </div>
              {selected.final_reply && (
                <div className="ra-subagent-final-reply-prose">
                  <ChatMarkdown>{selected.final_reply}</ChatMarkdown>
                </div>
              )}
              {selected.notification_payload && (
                <details className="ra-subagent-notif-details">
                  <summary>{tr("查看原始 &lt;subagent_notification&gt; 结构化载荷")}</summary>
                  <pre className="ra-subagent-notif-pre">
                    {`<subagent_notification>\n${JSON.stringify(
                      selected.notification_payload,
                      null,
                      2,
                    )}\n</subagent_notification>`}
                  </pre>
                </details>
              )}
            </div>
          )}
        </div>

        {/* Sticky Bottom Action Bar */}
        <div className="ra-subagent-drawer-footer">
          {running || waiting ? (
            <>
              <div className="ra-subagent-footer-status">
                <span className="ra-subagent-dot is-running" />
                <span>{tr("主智能体正在后台监听此子智能体")}</span>
              </div>
              <div className="ra-subagent-footer-actions">
                <button
                  type="button"
                  className="ra-subagent-footer-btn is-danger"
                  onClick={() => onInterruptSubagent(selected.id)}
                > {tr("中断当前轮次")} </button>
                <button
                  type="button"
                  className="ra-subagent-footer-btn is-primary"
                  onClick={() => onMentionSubagent(selected.nickname)}
                > {tr("在主对话中 @")}{selected.nickname}
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="ra-subagent-footer-status">
                <span>{tr("📦 调查产物已归档至主会话记忆库")}</span>
              </div>
              <div className="ra-subagent-footer-actions">
                <button
                  type="button"
                  className="ra-subagent-footer-btn"
                  onClick={() => {
                    const textToCopy = selected.final_reply || selected.current_step_text || "";
                    if (textToCopy && navigator.clipboard) {
                      navigator.clipboard.writeText(textToCopy).catch(() => {});
                      setCopiedId(selected.id);
                      setTimeout(() => setCopiedId(null), 1800);
                    }
                  }}
                >
                  {copiedId === selected.id ? "✓ 已复制结论" : "复制结论"}
                </button>
                <button
                  type="button"
                  className="ra-subagent-footer-btn is-primary"
                  onClick={() =>
                    onMentionSubagent(selected.nickname, selected.final_reply || undefined)
                  }
                > {tr("在主会话中引用")} </button>
              </div>
            </>
          )}
        </div>
      </aside>
    );
  }

  return (
    <aside className="ra-subagent-drawer" aria-label={tr("子智能体总览面板")}>
      <div className="ra-subagent-drawer-header">
        <div>
          <h3 className="ra-subagent-drawer-title">{tr("子智能体")}</h3>
          <p className="ra-subagent-drawer-subtitle"> {tr("共")} {subagents.length} {tr("个子智能体 · 实时追踪任务分工与历史记录")} </p>
        </div>
        <button
          type="button"
          className="ra-subagent-close-btn"
          onClick={onClose}
          aria-label={tr("关闭子智能体面板")}
        >
          ×
        </button>
      </div>

      <div className="ra-subagent-drawer-body">
        {/* Section 1: Active / Running / Waiting */}
        <div className="ra-subagent-list-group">
          <div className="ra-subagent-list-group-header">
            <span className="ra-subagent-group-label">
              <span className="ra-subagent-dot is-running" /> {tr("已开启 ·")} {activeList.length}
            </span>
            {waitingCount > 0 && (
              <span className="ra-subagent-waiting-badge">{waitingCount} {tr("个等待中")}</span>
            )}
          </div>

          {activeList.length === 0 ? (
            <div className="ra-subagent-empty-card">{tr("当前没有正在运行的子智能体")}</div>
          ) : (
            activeList.map(sub => {
              const running = isSubagentRunning(sub);
              const added = sub.diff_stats?.added || 0;
              const removed = sub.diff_stats?.removed || 0;
              const zeroDiff = added === 0 && removed === 0;
              return (
                <div
                  key={sub.id}
                  className={`ra-subagent-overview-card ${
                    sub.is_waiting ? "is-waiting" : running ? "is-running" : ""
                  }`}
                  onClick={() => onSelectSubagent(sub.id)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={e => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onSelectSubagent(sub.id);
                    }
                  }}
                >
                  <div className="ra-subagent-card-top">
                    <div className="ra-subagent-card-identity">
                      <SubagentPixelAvatar nickname={sub.nickname} size={20} />
                      <span className="ra-subagent-card-nick">{sub.nickname}</span>
                      <span className={`ra-subagent-role-badge is-${roleLabel(sub.role)}`}>
                        {roleLabel(sub.role)}
                      </span>
                      <span className="ra-subagent-card-path">{sub.agent_path}</span>
                    </div>
                    <div className="ra-subagent-card-metrics">
                      {sub.is_waiting && (
                        <span className="ra-subagent-waiting-pill">{tr("等待中")}</span>
                      )}
                      <span className="ra-subagent-card-timer">
                        {formatDurationClock((sub.elapsed_seconds || 0) + (running ? liveTickSec : 0))}
                      </span>
                      {!zeroDiff && (
                        <span className="ra-subagent-diff-stat">
                          <span className="is-add">+{added}</span>
                          <span className="is-del">-{removed}</span>
                        </span>
                      )}
                    </div>
                  </div>

                  <div className="ra-subagent-card-objective">{sub.instructions}</div>

                  <div className="ra-subagent-card-progress-track">
                    <div
                      className={`ra-subagent-card-progress-fill ${
                        sub.is_waiting ? "is-waiting" : "is-running"
                      }`}
                      style={{ width: `${Math.min(100, Math.max(8, sub.progress_percent || 20))}%` }}
                    />
                  </div>

                  <div className="ra-subagent-card-step-row">
                    <span className="ra-subagent-card-step">
                      {sub.step_progress_label || sub.current_step_text}
                    </span>
                    <span className="ra-subagent-card-chevron">›</span>
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* Section 2: Completed / Settled */}
        {completedList.length > 0 && (
          <div className="ra-subagent-list-group">
            <div className="ra-subagent-list-group-header">
              <span className="ra-subagent-group-label">
                <span className="ra-subagent-dot is-completed" /> {tr("完成 ·")} {completedList.length}
              </span>
              {avgCompletedSec > 0 && (
                <span className="ra-subagent-avg-time">{tr("耗时均值")} {avgCompletedSec}{tr("s")}</span>
              )}
            </div>

            {completedList.map(sub => {
              const isCompletedOk = sub.status === "completed";
              const statusLabel = isCompletedOk
                ? "✓ 已完成"
                : sub.status === "paused"
                ? "⏸ 已中断"
                : sub.status === "failed"
                ? "✕ 执行失败"
                : sub.status === "cancelled"
                ? "⊘ 已取消"
                : sub.status;
              return (
                <div
                  key={sub.id}
                  className={`ra-subagent-overview-card ${isCompletedOk ? "is-completed" : "is-settled"}`}
                  onClick={() => onSelectSubagent(sub.id)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={e => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onSelectSubagent(sub.id);
                    }
                  }}
                >
                  <div className="ra-subagent-card-top">
                    <div className="ra-subagent-card-identity">
                      <SubagentPixelAvatar nickname={sub.nickname} size={18} />
                      <span className="ra-subagent-card-nick">{sub.nickname}</span>
                      <span className={`ra-subagent-role-badge is-${roleLabel(sub.role)}`}>
                        {roleLabel(sub.role)}
                      </span>
                    </div>
                    <div className="ra-subagent-card-metrics">
                      <span className={`ra-subagent-completed-time${isCompletedOk ? "" : " is-muted"}`}>
                        {statusLabel} {tr("· 用时")} {formatDurationShort(sub.elapsed_seconds)} · {formatRelativeCompletedTime(sub.completed_at_ms)}
                      </span>
                      {(sub.diff_stats?.added > 0 || sub.diff_stats?.removed > 0) && (
                        <span className="ra-subagent-diff-stat">
                          <span className="is-add">+{sub.diff_stats.added}</span>
                          <span className="is-del">-{sub.diff_stats.removed}</span>
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="ra-subagent-card-summary">
                    {sub.conclusion_preview || sub.final_reply || sub.current_step_text || sub.instructions}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </aside>
  );
}
