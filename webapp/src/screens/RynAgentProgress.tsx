import { useContext, useEffect, useMemo, useState } from "react";
import { ChevronRight, Clock3 } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { AgentMessage, AgentTask } from "../domain/rynAgent";
import { agentTurnTiming } from "./rynAgentTiming";
import { AgentFileContext } from "./RynAgentFileContext";
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
  return <div className={`ra-turn-duration${timing.live ? " ra-turn-duration-live" : ""}`} title={t(timing.approximate ? "turnDurationEstimatedHelp" : "turnDurationHelp")}>
    <Clock3 size={12} aria-hidden="true" /><span role="timer" aria-live="off" aria-label={t("turnDurationLabel")}>{label}</span>
  </div>;
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
  const { t } = useTranslation("rynAgent");
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
  const calls = new Set(messages.flatMap(message => message.tool_calls?.map(call => call.id) || []));
  const operations = new Set(task?.events.filter(event => typeof event.call_id === "string" && calls.has(event.call_id)).map(event => event.operation_id));
  const observations = fileContext?.files.filter(file =>
    (file.source === "computer_observation" || file.path.replaceAll("\\", "/").startsWith(".ryn/computer/")) &&
    operations.has(file.operation_id || file.path.replaceAll("\\", "/").split("/").at(-2))) || [];
  const count = messages.reduce((total, message) => total + (message.tool_calls?.length || 0), 0);
  return <div className="ra-process">
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
  </div>;
}
