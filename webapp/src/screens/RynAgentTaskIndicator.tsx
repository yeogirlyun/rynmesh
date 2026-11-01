import { CircleAlert, Clock3, LoaderCircle, Pause, TriangleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";
import { agentTaskState, type AgentTaskSummary } from "../domain/rynAgent";
import "./RynAgentTaskIndicator.css";

/** Compact attention hints shared by every sidebar task row. */
export default function RynAgentTaskIndicator({ task, unread = false }: { task: AgentTaskSummary; unread?: boolean }) {
  const { t } = useTranslation("rynAgent");
  if (task.archived) return null;

  const state = agentTaskState(task);
  if (task.pending_approval || state === "waiting_approval") {
    const label = t("sidebarNeedsApproval", { defaultValue: "Needs confirmation" });
    return <span className="ra-task-indicator ra-task-indicator-badge ra-task-indicator-approval" title={label}>{label}</span>;
  }
  if (task.pending_input || state === "waiting_input") {
    const label = t("sidebarNeedsInput", { defaultValue: "Awaiting your answer" });
    return <span className="ra-task-indicator ra-task-indicator-badge ra-task-indicator-input" title={label}>{label}</span>;
  }
  if (state === "completed" && unread) {
    const label = t("sidebarUnreadCompletion", { defaultValue: "Unread completed reply" });
    return <span className="ra-task-indicator ra-task-indicator-unread" role="img" aria-label={label} title={label}><span className="ra-task-unread-dot" aria-hidden="true" /></span>;
  }
  if (["running", "pausing", "reconciling"].includes(state)) {
    const label = t(state);
    return <span className="ra-task-indicator ra-task-indicator-running" role="img" aria-label={label} title={label}><LoaderCircle className="ra-task-indicator-spinner" size={13} aria-hidden="true" /></span>;
  }
  const Icon = state === "queued" ? Clock3 : state === "paused" ? Pause : state === "failed" ? CircleAlert : state === "interrupted" ? TriangleAlert : null;
  if (!Icon) return null;
  const label = t(state);
  return <span className={`ra-task-indicator ra-task-indicator-${state}`} role="img" aria-label={label} title={label}><Icon size={13} aria-hidden="true" /></span>;
}
