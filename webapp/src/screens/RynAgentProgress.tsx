import { useContext, useEffect, useMemo, useState } from "react";
import { ChevronRight, Clock3 } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { AgentMessage, AgentTask } from "../domain/rynAgent";
import { agentTurnTiming } from "./rynAgentTiming";
import { AgentFileContext, fileAvailable, findAgentFile } from "./RynAgentFileContext";
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

export default function RynAgentProgress({ messages, task }: { messages: AgentMessage[]; task?: AgentTask }) {
  const { t, i18n } = useTranslation("rynAgent");
  const zh = i18n.language.startsWith("zh");
  const fileContext = useContext(AgentFileContext);
  const calls = new Set(messages.flatMap(message => message.tool_calls?.map(call => call.id) || []));
  const operations = new Set(task?.events.filter(event => typeof event.call_id === "string" && calls.has(event.call_id)).map(event => event.operation_id));
  const observations = fileContext?.files.filter(file =>
    (file.source === "computer_observation" || file.path.replaceAll("\\", "/").startsWith(".ryn/computer/")) &&
    operations.has(file.operation_id || file.path.replaceAll("\\", "/").split("/").at(-2))) || [];
  const count = messages.reduce((total, message) => total + (message.tool_calls?.length || 0), 0);
  const fileEdits = useMemo(() => extractFileEdits(messages), [messages]);
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
    <details className="ra-process-details">
      <summary><ChevronRight size={13} /><span>{t("processDetails")}</span><small>{t("processCount", { count })}</small></summary>
      <div className="ra-process-entries">{messages.map((message, index) => <div className="ra-process-entry" key={index}>
        <div className="ra-process-tools">{message.tool_calls?.map(call => {
          const detail = summarizeCallTarget(call.function.arguments);
          return <span key={call.id} className="ra-process-tool-chip">{t(`tool_${call.function.name}`, { defaultValue: call.function.name })}{detail && <em className="ra-process-tool-arg" aria-hidden="true">{detail}</em>}</span>;
        })}</div>
      </div>)}{observations.length > 0 && <div className="ra-process-observations">{observations.map((file, index) =>
        <button key={file.id} onClick={() => fileContext?.open(file)} disabled={file.status === "missing"}>
          {t("computerObservation", { count: index + 1 })}
        </button>)}</div>}</div>
    </details>
  </div>;
}
