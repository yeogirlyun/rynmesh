import { useEffect, useState } from "react";
import { Check, LoaderCircle, Pause, WifiOff } from "lucide-react";
import { useTranslation } from "react-i18next";
import { canUseAgentAction, type AgentTaskSummary } from "../domain/rynAgent";

export function hasConnectionRecovery(task: AgentTaskSummary) {
  const state = task.connection_recovery?.state;
  if (!state || task.error || task.archived) return false;
  if (state === "stopped" || state === "disconnected") return task.status === "paused";
  return (state === "retrying" ? ["queued", "running"] : ["queued", "running", "completed", "waiting_approval", "waiting_input"]).includes(task.status);
}

export default function RynAgentConnection({ task, modelName, busy, enabled, onAction }: {
  task: AgentTaskSummary; modelName: string; busy: boolean; enabled: boolean;
  onAction: (action: "pause" | "resume") => void;
}) {
  const { i18n } = useTranslation();
  const zh = i18n.language.startsWith("zh");
  const recovery = task.connection_recovery;
  const recoveredAt = recovery?.recovered_at;
  const [clock, setClock] = useState(Date.now());
  useEffect(() => {
    setClock(Date.now());
    if (!recoveredAt) return;
    const timer = setTimeout(() => setClock(Date.now()), Math.max(0, recoveredAt * 1000 + 3000 - Date.now()));
    return () => clearTimeout(timer);
  }, [task.id, recoveredAt]);
  if (!hasConnectionRecovery(task) || !recovery) return null;
  if (recovery.state === "recovered") {
    return clock < (recoveredAt || 0) * 1000 + 3000 ? <p className="ra-connection-recovered" role="status"><Check size={15} />{zh ? "连接已恢复" : "Connection restored"}</p> : null;
  }
  const retrying = recovery.state === "retrying" && ["queued", "running"].includes(task.status);
  const stopped = recovery.state === "stopped";
  const blocked = recovery.max_retries === 0;
  const reason = recovery.diagnostics?.network_reason;
  const status = recovery.diagnostics?.http_status;
  const description = retrying
    ? (zh ? "连接暂时中断，恢复后会自动继续。" : "Connection interrupted. Your task will continue automatically once restored.")
    : stopped
      ? (zh ? "当前进度已保留，等待你继续。" : "Your progress is saved. Continue when you are ready.")
      : blocked
        ? (zh ? reason === "quota" ? "模型服务额度不足。请检查余额或额度设置后继续。" : reason === "tls_certificate" || reason === "tls" ? "安全连接校验未通过。请检查证书或代理设置后继续。" : reason === "dns_not_found" ? "无法解析服务地址。请检查模型地址或网络设置后继续。" : status === 401 || status === 403 ? "服务拒绝了访问。请检查 API 密钥和访问权限后继续。" : "服务暂时无法接受请求。请检查模型服务配置后继续。"
          : "The service could not accept the request. Check its credentials, address and connection settings before continuing.")
        : (zh ? `已自动重试 ${recovery.retry_count} 次。当前进度已保留，等待你继续。` : `Retried ${recovery.retry_count} times. Your progress is saved.`);
  const Icon = retrying ? LoaderCircle : stopped ? Pause : WifiOff;
  return <section className={`ra-connection ${retrying ? "is-retrying" : stopped ? "is-stopped" : "is-disconnected"}`} aria-label={zh ? "模型连接状态" : "Model connection status"}>
    <Icon size={16} className={retrying ? "ra-spinner" : undefined} aria-hidden="true" />
    <div className="ra-connection-body">
      <div role="status" aria-live="polite">
        <div className="ra-connection-heading"><strong>{retrying ? (zh ? `正在重新连接 ${modelName}` : `Reconnecting to ${modelName}`) : stopped ? (zh ? "已停止重连" : "Reconnection stopped") : (zh ? `暂时无法连接 ${modelName}` : `Unable to connect to ${modelName}`)}</strong>
          {retrying && recovery.retry_count > 0 && <small>{zh ? "重试" : "Retry"} {recovery.retry_count} / {recovery.max_retries}</small>}</div>
        <p>{description}</p>
      </div>
      <button type="button" className={retrying ? "ra-connection-stop" : "ra-primary"}
        disabled={busy || !canUseAgentAction(task, retrying ? "pause" : "resume", enabled)}
        onClick={() => onAction(retrying ? "pause" : "resume")}>
        {retrying ? (zh ? "停止重试" : "Stop retrying") : (zh ? "重试并继续" : "Retry and continue")}
      </button>
    </div>
  </section>;
}
