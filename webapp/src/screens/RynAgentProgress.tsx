import { useContext, useEffect, useMemo, useState } from "react";
import { ChevronRight, Clock3 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { agentTaskState, type AgentEvent, type AgentMessage, type AgentTask } from "../domain/rynAgent";
import { agentTurnTiming } from "./rynAgentTiming";
import { AgentFileContext, fileAvailable, findAgentFile } from "./RynAgentFileContext";
import { extractSubagentCallBatchesFromMessages, isSubagentToolName, SubagentInlineGroup } from "./RynAgentSubagents";
import "./RynAgentProgress.css";

type TranscriptEntry = ({ kind: "message"; message: AgentMessage } | { kind: "progress"; messages: AgentMessage[] }) & { turnIndex: number };

export function groupAgentMessages(messages: AgentMessage[]): TranscriptEntry[] {
  const entries: TranscriptEntry[] = [];
  let turnIndex = -1;
  for (const message of messages) {
    if (message.role === "user") turnIndex += 1;
    if (message.role === "assistant" && message.tool_calls?.length) {
      // Public assistant commentary belongs to the conversation even when the
      // same response dispatches tools. Keep only the operations collapsible.
      if (message.content?.trim()) entries.push({ kind: "message", message, turnIndex });
      const previous = entries.at(-1);
      if (previous?.kind === "progress") previous.messages.push(message);
      else entries.push({ kind: "progress", messages: [message], turnIndex });
    } else entries.push({ kind: "message", message, turnIndex });
  }
  return entries;
}

interface ParsedFileEdit {
  id: string;
  path: string;
  name: string;
  added: number;
  removed: number;
}

function extractFileEdits(messages: AgentMessage[]): ParsedFileEdit[] {
  const edits = new Map<string, ParsedFileEdit>();
  for (const msg of messages) {
    for (const call of msg.tool_calls || []) {
      const fn = call.function.name;
      if (!["ryn_file_write", "ryn_file_edit", "ryn_apply_patch", "ryn_create_text_file"].includes(fn)) continue;
      try {
        const args = JSON.parse(call.function.arguments || "{}") as Record<string, unknown>;
        const rawPath = String(args.path || args.file_path || args.target || "");
        const patch = String(args.patch || args.diff || "");
        let targetPath = rawPath;
        if (!targetPath && patch) {
          const match = patch.match(/\*\*\* (?:Update|Add) File:\s*([^\r\n]+)/);
          if (match) targetPath = match[1].trim();
        }
        if (!targetPath) continue;
        const name = targetPath.replaceAll("\\", "/").split("/").filter(Boolean).pop() || targetPath;
        let added = 0;
        let removed = 0;
        if (patch) {
          for (const line of patch.split(/\r?\n/)) {
            if (line.startsWith("+") && !line.startsWith("+++")) added++;
            else if (line.startsWith("-") && !line.startsWith("---")) removed++;
          }
        } else if (typeof args.content === "string") {
          added = Math.max(1, args.content.split(/\r?\n/).length);
        } else if (typeof args.replacement === "string" || typeof args.new_text === "string") {
          const nextText = String(args.replacement ?? args.new_text ?? "");
          const prevText = String(args.target ?? args.old_text ?? "");
          added = nextText ? nextText.split(/\r?\n/).length : 0;
          removed = prevText ? prevText.split(/\r?\n/).length : 0;
        }
        const prev = edits.get(name);
        edits.set(name, {
          id: call.id,
          path: targetPath,
          name,
          added: (prev?.added || 0) + added,
          removed: (prev?.removed || 0) + removed,
        });
      } catch {
        /* Ignore malformed tool call arguments */
      }
    }
  }
  return [...edits.values()];
}

function summarizeCallTarget(rawArgs: string): string {
  try {
    const args = JSON.parse(rawArgs || "{}") as Record<string, unknown>;
    const candidate = args.path || args.file_path || args.command || args.cmd || args.query || args.url || "";
    if (typeof candidate !== "string" || !candidate.trim()) return "";
    const clean = candidate.trim().replaceAll("\\", "/");
    if (clean.includes("/") && !clean.includes(" ")) {
      return clean.split("/").slice(-2).join("/");
    }
    return clean.length > 48 ? `${clean.slice(0, 48)}…` : clean;
  } catch {
    return "";
  }
}

const COMPACTION_TURN_EVENTS = new Set(["created", "message_added", "goal_changed", "information_added", "input_answered"]);
const COMPACTION_STOP_EVENTS = new Set(["completed", "failed", "cancelled", "interrupted", "paused", "connection_paused", "approval_requested", "input_requested", "run_settled"]);
const COMPACTION_DELAY_NOTICE_MS = 10_000;

export interface CodexCompactionItem {
  id: string;
  seq: number;
  turnIndex: number;
  afterCallId: string | null;
  status: "inProgress" | "interrupted" | "completed";
  startedAtMs: number | null;
  source: "manual" | "automatic";
}

function parseEventMs(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value < 1e12 ? value * 1000 : value;
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

export function extractCodexCompactions(task: AgentTask | undefined): CodexCompactionItem[] {
  if (!task?.events?.length) return [];
  const events = [...task.events].sort((left, right) => left.seq - right.seq);
  const boundaries: AgentEvent[] = [];
  for (const ev of events) {
    if (COMPACTION_TURN_EVENTS.has(ev.type)) boundaries.push(ev);
    else if (ev.type === "message_edited" && typeof ev.user_turn_index === "number" && Number.isInteger(ev.user_turn_index) && ev.user_turn_index >= 0) {
      boundaries.splice(ev.user_turn_index);
      boundaries.push(ev);
    }
  }
  const activeRun = ["running", "queued", "pausing", "reconciling"].includes(agentTaskState(task));
  const result: CodexCompactionItem[] = [];
  const sliceCount = Math.max(1, boundaries.length);
  for (let turnIndex = 0; turnIndex < sliceCount; turnIndex += 1) {
    const start = boundaries[turnIndex];
    const nextOriginal = start
      ? events.find(ev => ev.seq > start.seq && (COMPACTION_TURN_EVENTS.has(ev.type) || ev.type === "message_edited"))
      : undefined;
    const scoped = start
      ? events.filter(ev => ev.seq >= start.seq && (!nextOriginal || ev.seq < nextOriginal.seq))
      : events;
    let lastCallId: string | null = null;
    const pairedCompactedSeqs = new Set<number>();
    for (let i = 0; i < scoped.length; i += 1) {
      const ev = scoped[i];
      if ((ev.type === "tool_started" || ev.type === "tool_result") && typeof ev.call_id === "string" && ev.call_id) {
        lastCallId = ev.call_id;
      } else if (ev.type === "context_compaction_started") {
        let status: CodexCompactionItem["status"] = activeRun && !nextOriginal ? "inProgress" : "interrupted";
        for (let j = i + 1; j < scoped.length; j += 1) {
          const nextEv = scoped[j];
          if (nextEv.type === "context_compaction_started") break;
          if (nextEv.type === "context_compacted") {
            status = "completed";
            pairedCompactedSeqs.add(nextEv.seq);
            break;
          }
          if (nextEv.type === "context_compaction_rejected" || COMPACTION_STOP_EVENTS.has(nextEv.type)) {
            status = "interrupted";
            break;
          }
        }
        result.push({
          id: `compaction-${ev.seq}`,
          seq: ev.seq,
          turnIndex,
          afterCallId: lastCallId,
          status,
          startedAtMs: parseEventMs(ev.at),
          source: (ev as { source?: unknown }).source === "manual" ? "manual" : "automatic",
        });
      } else if (ev.type === "context_compacted" && !pairedCompactedSeqs.has(ev.seq)) {
        result.push({
          id: `compaction-${ev.seq}`,
          seq: ev.seq,
          turnIndex,
          afterCallId: lastCallId,
          status: "completed",
          startedAtMs: parseEventMs(ev.at),
          source: (ev as { source?: unknown }).source === "manual" ? "manual" : "automatic",
        });
      }
    }
  }
  return result;
}

/** Codex text-select-light-16 (m2n) monochrome 16×16 asset from app-shared. */
function CodexCompactionIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true" className="ra-codex-compaction-icon">
      <path d="M11.5247 1.80859C12.9925 1.80873 14.1826 2.99992 14.1829 4.46777V11.752C14.6643 11.9563 15.0021 12.4342 15.0022 12.9902C15.0021 13.7322 14.4004 14.3346 13.6584 14.335C12.9162 14.335 12.3139 13.7324 12.3137 12.9902C12.3138 12.4342 12.6513 11.9563 13.1331 11.752V4.46777C13.1328 3.57982 12.4126 2.85951 11.5247 2.85938L8.86646 2.86035C8.57667 2.86035 8.34133 2.62469 8.34106 2.33496C8.34106 2.04501 8.57651 1.80859 8.86646 1.80859H11.5247Z" fill="currentColor" />
      <path d="M2.33228 1.75488C3.0744 1.75501 3.67589 2.35651 3.67603 3.09863C3.67603 3.65479 3.33827 4.13233 2.85669 4.33691L2.85767 11.5234C2.85793 12.4115 3.57797 13.1318 4.46606 13.1318H7.12427C7.41399 13.1321 7.64966 13.3674 7.64966 13.6572C7.64939 13.9468 7.41383 14.1824 7.12427 14.1826H4.46606C2.99807 14.1826 1.80813 12.9914 1.80786 11.5234L1.80688 4.33691C1.32538 4.13221 0.987549 3.65459 0.987549 3.09863C0.987681 2.35642 1.59004 1.75488 2.33228 1.75488Z" fill="currentColor" />
      <path d="M8.66626 8.89258C8.95621 8.89258 9.19165 9.12802 9.19165 9.41797C9.19165 9.70792 8.95621 9.94336 8.66626 9.94336H5.66626C5.37648 9.94316 5.14087 9.7078 5.14087 9.41797C5.14087 9.12814 5.37648 8.89278 5.66626 8.89258H8.66626Z" fill="currentColor" />
      <path d="M10.3333 6.05957C10.6231 6.05975 10.8577 6.29512 10.8577 6.58496C10.8575 6.87469 10.623 7.11018 10.3333 7.11035H5.66626C5.37656 7.11015 5.141 6.87468 5.14087 6.58496C5.14087 6.29513 5.37648 6.05977 5.66626 6.05957H10.3333Z" fill="currentColor" />
    </svg>
  );
}

/** Codex $C + xg + ew activity row for context-compaction items. */
export function RynContextCompactionRow({ item }: { item: CodexCompactionItem }) {
  const { i18n } = useTranslation("rynAgent");
  const zh = i18n.language.startsWith("zh");
  const [delayed, setDelayed] = useState(() => item.startedAtMs != null && Date.now() - item.startedAtMs >= COMPACTION_DELAY_NOTICE_MS);
  useEffect(() => {
    if (item.status !== "inProgress" || item.startedAtMs == null || delayed) return;
    const wait = Math.max(0, item.startedAtMs + COMPACTION_DELAY_NOTICE_MS - Date.now());
    const timer = window.setTimeout(() => setDelayed(true), wait);
    return () => window.clearTimeout(timer);
  }, [item.status, item.startedAtMs, delayed]);

  let label: string;
  if (item.status === "inProgress") {
    label = delayed
      ? (zh ? "正在压缩上下文 · 可能需要几分钟" : "Compacting context · This can take a few minutes")
      : (zh ? "正在压缩上下文" : "Compacting context");
  } else if (item.status === "interrupted") {
    label = zh ? "压缩已中断" : "Compaction interrupted";
  } else if (item.source === "manual") {
    label = zh ? "上下文已压缩" : "Context compacted";
  } else {
    label = zh ? "上下文已自动压缩" : "Context automatically compacted";
  }

  return (
    <div className="ra-codex-compaction-row" data-testid="context-compaction-row">
      <div className="ra-codex-compaction-header">
        <span className="ra-codex-compaction-inner">
          <span className="ra-codex-compaction-icon-wrap">
            <CodexCompactionIcon />
          </span>
          <span className={`ra-codex-compaction-summary${item.status === "inProgress" ? " is-shimmer" : ""}`}>
            {label}
          </span>
        </span>
      </div>
    </div>
  );
}

/** Render once just after each user message so timing appears before the first
 * token and remains attached to the same turn after completion or reload. */
export function RynAgentTurnDuration({ task, turnIndex }: { task: AgentTask; turnIndex: number }) {
  const { t } = useTranslation("rynAgent");
  const timing = useMemo(() => agentTurnTiming(task, turnIndex), [task, turnIndex]);
  const snapshotAt = useMemo(() => performance.now(), [task.run_timing, task.events, turnIndex]);
  const [now, setNow] = useState(snapshotAt);
  useEffect(() => {
    if (!timing.live) return;
    const timer = window.setInterval(() => setNow(performance.now()), 1000);
    return () => window.clearInterval(timer);
  }, [timing.live]);
  // Interpolate only a server-confirmed live clock, for at most one polling
  // gap. A disconnected/stale snapshot must not tick indefinitely.
  const elapsed = (timing.seconds ?? 0) + (timing.live ? Math.min(3, Math.max(0, now - snapshotAt) / 1000) : 0);
  const whole = Math.floor(elapsed);
  const hours = Math.floor(whole / 3600), minutes = Math.floor(whole % 3600 / 60), secs = whole % 60;
  const duration = [hours ? t("durationHours", { count: hours }) : "", hours || minutes ? t("durationMinutes", { count: minutes }) : "", t("durationSeconds", { count: secs })].filter(Boolean).join(" ");
  const label = timing.seconds === null ? t("turnDurationUnknown")
    : t(`turnDuration_${timing.state}`, { duration: timing.approximate ? t("durationApproximate", { duration }) : duration });
  const preCallCompactions = useMemo(
    () => extractCodexCompactions(task).filter(c => c.turnIndex === turnIndex && c.afterCallId === null),
    [task, turnIndex],
  );
  return <>
    <div className={`ra-turn-duration${timing.live ? " ra-turn-duration-live" : ""}`} title={t(timing.approximate ? "turnDurationEstimatedHelp" : "turnDurationHelp")}>
      <Clock3 size={12} aria-hidden="true" /><span role="timer" aria-live="off" aria-label={t("turnDurationLabel")}>{label}</span>
    </div>
    {preCallCompactions.map(item => <RynContextCompactionRow key={item.id} item={item} />)}
  </>;
}

export default function RynAgentProgress({
  messages,
  task,
  onSelectSubagent,
}: {
  messages: AgentMessage[];
  task?: AgentTask;
  onSelectSubagent?: (subagentId: string) => void;
}) {
  const { t, i18n } = useTranslation("rynAgent");
  const zh = i18n.language.startsWith("zh");
  const fileContext = useContext(AgentFileContext);
  const subagentBatches = useMemo(
    () => extractSubagentCallBatchesFromMessages(messages, task?.events || [], task?.subagents || []),
    [messages, task?.events, task?.subagents],
  );
  const nonSubagentCount = messages.reduce(
    (total, message) =>
      total + (message.tool_calls?.filter(tc => !isSubagentToolName(tc.function.name)).length || 0),
    0,
  );
  const calls = useMemo(() => new Set(messages.flatMap(message => message.tool_calls?.map(call => call.id) || [])), [messages]);
  const postCallCompactions = useMemo(
    () => extractCodexCompactions(task).filter(c => c.afterCallId !== null && calls.has(c.afterCallId)),
    [task, calls],
  );
  const operations = new Set(task?.events.filter(event => typeof event.call_id === "string" && calls.has(event.call_id)).map(event => event.operation_id));
  const observations = fileContext?.files.filter(file =>
    (file.source === "computer_observation" || file.path.replaceAll("\\", "/").startsWith(".ryn/computer/")) &&
    operations.has(file.operation_id || file.path.replaceAll("\\", "/").split("/").at(-2))) || [];
  const count = messages.reduce((total, message) => total + (message.tool_calls?.length || 0), 0);
  const fileEdits = useMemo(() => extractFileEdits(messages).filter(edit =>
    task?.events.some(event => event.type === "tool_result" && event.call_id === edit.id &&
      event.result && typeof event.result === "object" &&
      ((event.result as Record<string, unknown>).ok === true || (event.result as Record<string, unknown>).success === true))),
    [messages, task?.events]);
  return <div className="ra-process">
    {fileEdits.length > 0 && (
      <div className="ra-edit-diff-list" aria-hidden="true">
        {fileEdits.map(edit => {
          const bound = fileContext ? findAgentFile(fileContext.files, edit.path) || findAgentFile(fileContext.files, edit.name) : undefined;
          const canOpen = Boolean(bound && fileAvailable(bound));
          return (
            <div
              key={edit.id}
              className={`ra-edit-diff-card${canOpen ? " is-clickable" : ""}`}
              onClick={canOpen && bound ? () => fileContext?.open(bound) : undefined}
            >
              <div className="ra-edit-diff-top">
                <span className="ra-edit-diff-verb">{zh ? "已编辑" : "Edited"}</span>
                <span className="ra-edit-diff-name">{edit.name}</span>
              </div>
              {(edit.added > 0 || edit.removed > 0) && (
                <div className="ra-edit-diff-stats">
                  {edit.added > 0 && <span className="ra-diff-add">+{edit.added}</span>}
                  {edit.removed > 0 && <span className="ra-diff-del">-{edit.removed}</span>}
                </div>
              )}
            </div>
          );
        })}
      </div>
    )}
    {subagentBatches.map((batch, bIdx) => (
      <SubagentInlineGroup
        key={batch[0]?.callId || `subagent-batch-${bIdx}`}
        calls={batch}
        subagents={task?.subagents || []}
        onSelectSubagent={onSelectSubagent}
      />
    ))}
    {(nonSubagentCount > 0 || subagentBatches.length === 0) && (
      <details className="ra-process-details">
        <summary><ChevronRight size={13} /><span>{t("processDetails")}</span><small>{t("processCount", { count: nonSubagentCount > 0 ? nonSubagentCount : count })}</small></summary>
        <div className="ra-process-entries">{messages.map((message, index) => {
          const filteredCalls = message.tool_calls?.filter(call => !isSubagentToolName(call.function.name)) || [];
          if (!filteredCalls.length) return null;
          return <div className="ra-process-entry" key={index}>
            <div className="ra-process-tools">{filteredCalls.map(call => {
              const detail = summarizeCallTarget(call.function.arguments);
              return <span key={call.id} className="ra-process-tool-chip">{t(`tool_${call.function.name}`, { defaultValue: call.function.name })}{detail && <em className="ra-process-tool-arg" aria-hidden="true">{detail}</em>}</span>;
            })}</div>
          </div>;
        })}{observations.length > 0 && <div className="ra-process-observations">{observations.map((file, index) =>
          <button key={file.id} onClick={() => fileContext?.open(file)} disabled={file.status === "missing"}>
            {t("computerObservation", { count: index + 1 })}
          </button>)}</div>}</div>
      </details>
    )}
    {postCallCompactions.map(item => <RynContextCompactionRow key={item.id} item={item} />)}
  </div>;
}
