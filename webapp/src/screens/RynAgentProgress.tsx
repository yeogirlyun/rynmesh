import { useContext, useEffect, useMemo, useState } from "react";
import { ChevronRight, Clock3 } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { AgentMessage, AgentTask } from "../domain/rynAgent";
import { agentTurnTiming } from "./rynAgentTiming";
import { AgentFileContext } from "./RynAgentFileContext";
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
    <Clock3 size={13} aria-hidden="true" /><span role="timer" aria-live="off" aria-label={t("turnDurationLabel")}>{label}</span>
  </div>;
}

export default function RynAgentProgress({ messages, task }: { messages: AgentMessage[]; task?: AgentTask }) {
  const { t } = useTranslation("rynAgent");
  const fileContext = useContext(AgentFileContext);
  const calls = new Set(messages.flatMap(message => message.tool_calls?.map(call => call.id) || []));
  const operations = new Set(task?.events.filter(event => typeof event.call_id === "string" && calls.has(event.call_id)).map(event => event.operation_id));
  const observations = fileContext?.files.filter(file =>
    (file.source === "computer_observation" || file.path.replaceAll("\\", "/").startsWith(".ryn/computer/")) &&
    operations.has(file.operation_id || file.path.replaceAll("\\", "/").split("/").at(-2))) || [];
  const count = messages.reduce((total, message) => total + (message.tool_calls?.length || 0), 0);
  return <div className="ra-process">
    <details className="ra-process-details">
      <summary><ChevronRight size={14} /><span>{t("processDetails")}</span><small>{t("processCount", { count })}</small></summary>
      <div className="ra-process-entries">{messages.map((message, index) => <div className="ra-process-entry" key={index}>
        <div className="ra-process-tools">{message.tool_calls?.map(call => <span key={call.id}>{t(`tool_${call.function.name}`, { defaultValue: call.function.name })}</span>)}</div>
      </div>)}{observations.length > 0 && <div className="ra-process-observations">{observations.map((file, index) =>
        <button key={file.id} onClick={() => fileContext?.open(file)} disabled={file.status === "missing"}>
          {t("computerObservation", { count: index + 1 })}
        </button>)}</div>}</div>
    </details>
  </div>;
}
