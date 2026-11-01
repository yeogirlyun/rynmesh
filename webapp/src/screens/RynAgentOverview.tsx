import { Check, ChevronDown, Circle, CircleAlert, ListChecks, LoaderCircle } from "lucide-react";
import { useTranslation } from "react-i18next";
import { agentTaskState, type AgentTask } from "../domain/rynAgent";

/** Uses server plan states only; finished steps are not a claim of verified success. */
export default function RynAgentOverview({ task }: { task: AgentTask }) {
  const { t, i18n } = useTranslation("rynAgent");
  const zh = i18n.language.startsWith("zh");
  const state = agentTaskState(task);
  const steps = task.plan?.steps || [];
  const done = steps.filter(step => step.status === "done").length;
  const current = steps.find(step => step.status === "running") || steps.find(step => step.status === "blocked");
  const attention = Boolean(task.pending_input || task.pending_approval) || ["waiting_input", "waiting_approval", "failed", "interrupted"].includes(state);
  const running = ["running", "queued", "reconciling", "pausing"].includes(state);
  const Icon = attention ? CircleAlert : running ? LoaderCircle : state === "completed" ? Check : Circle;
  const label = task.pending_approval ? t("sidebarNeedsApproval") : task.pending_input ? t("sidebarNeedsInput") : t(state);
  const status = <span className={`ra-overview-state${attention ? " needs-attention" : running ? " is-running" : ""}`}><Icon size={14} className={running && !attention ? "ra-spinner" : undefined} /><span>{label}</span></span>;
  const summary = <>{status}<span className="ra-overview-current">{attention ? (zh ? "请查看下方说明" : "See the details below") : (running ? current?.text : undefined) || (steps.length ? t("plan") : zh ? "任务动态" : "Task activity")}</span>{steps.length > 0 && <span className="ra-overview-count">{done}/{steps.length}<span> {zh ? "步骤" : "steps"}</span></span>}</>;
  if (!steps.length) return <div className="ra-run-status" aria-label={zh ? "任务动态" : "Task activity"}>{summary}</div>;
  return <details className="ra-run-overview" key={task.id}>
    <summary aria-label={zh ? "查看任务进度" : "View task progress"}>{summary}<ChevronDown className="ra-overview-chevron" size={14} /></summary>
    <div className="ra-overview-details">
      <div className="ra-overview-heading"><ListChecks size={15} /><strong>{t("plan")}</strong><span>{zh ? "随执行更新" : "Updated as work progresses"}</span></div>
      <ol>{steps.map(step => {
        const StepIcon = step.status === "done" ? Check : step.status === "running" ? LoaderCircle : step.status === "blocked" ? CircleAlert : Circle;
        return <li key={step.id} data-state={step.status}><StepIcon size={15} className={step.status === "running" ? "ra-spinner" : undefined} /><span>{step.text}</span><small>{t(`plan_${step.status}`)}</small></li>;
      })}</ol>
      <p>{t("planHelp")}</p>
    </div>
  </details>;
}
