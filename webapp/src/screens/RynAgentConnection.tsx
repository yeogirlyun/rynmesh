import { useEffect, useState } from "react";
import { Check, LoaderCircle, Pause, WifiOff } from "lucide-react";
import { useTranslation } from "react-i18next";
import { canUseAgentAction, type AgentEvent, type AgentTaskSummary } from "../domain/rynAgent";

export function hasConnectionRecovery(task: AgentTaskSummary) {
  const state = task.connection_recovery?.state;
  if (!state || task.error || task.archived) return false;
  if (state === "stopped" || state === "disconnected") return task.status === "paused";
  if (state === "retrying" && (task.connection_recovery?.retry_count ?? 0) < 2) return false;
  return (state === "retrying" ? ["queued", "running"] : ["queued", "running", "completed", "waiting_approval", "waiting_input"]).includes(task.status);
}

type ConnectionProps = {
  task: AgentTaskSummary & { events?: AgentEvent[] }; modelName: string; busy: boolean; enabled: boolean;
  onAction: (action: "pause" | "resume") => void;
};

export default function RynAgentConnection(props: ConnectionProps) {
  const { i18n } = useTranslation();
  const zh = i18n.language.startsWith("zh");
  const { task } = props;
  const mismatch = task.events?.find(event => event.type === "model_mismatch");
  const continued = task.events?.some(event => event.type === "interrupted" && event.continued === true);
  const failure = task.error ? [...(task.events || [])].reverse().find(event => event.type === "failed") : undefined;
  const diagnostics = failure?.diagnostics as { network_reason?: string; reset_at?: number } | undefined;
  const reset = diagnostics?.reset_at;
  const seconds = typeof reset === "number" && Number.isFinite(reset) ? Math.max(0, Math.ceil(reset - Date.now() / 1000)) : null;
  return <>
    {continued && <p role="status">{zh ? "已自动续跑" : "Interrupted work resumed automatically"}</p>}
    {mismatch && <p role="note">{zh ? "服务端返回的模型与请求不同" : "The server returned a different model"}: {String(mismatch.requested_model)} → {String(mismatch.server_model)}</p>}
    {diagnostics?.network_reason === "quota" && seconds !== null && <p role="status">{zh ? `模型额度已用完，约 ${seconds} 秒后恢复。` : `Model quota exhausted; expected to recover in about ${seconds}s.`}</p>}
    {diagnostics?.network_reason === "region_blocked" && <p role="status">{zh ? "该线路在当前地区被拦截，请换线路" : "This route is blocked in your region. Choose another route."}</p>}
    <ConnectionRecovery {...props} />
  </>;
}

function ConnectionRecovery({ task, modelName, busy, enabled, onAction }: {
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
  const overloaded = recovery.diagnostics?.overloaded === true;
  const retryAfter = recovery.diagnostics?.retry_after_seconds;
  const retryAfterSecs = typeof retryAfter === "number" && Number.isFinite(retryAfter) && retryAfter > 0 ? Math.ceil(retryAfter) : null;
  let description: string;
  if (retrying) {
    if (reason === "idle_timeout" || reason === "timeout") {
      description = zh ? "模型长时间没有响应，正在自动重试。" : "The model has not responded for a long time. Retrying automatically.";
    } else if (status === 429) {
      description = retryAfterSecs !== null
        ? (zh ? `请求太频繁，限流中，约 ${retryAfterSecs} 秒后重试。` : `Too many requests. Rate-limited; retrying in about ${retryAfterSecs}s.`)
        : (zh ? "请求太频繁，限流中，稍后自动重试。" : "Too many requests. Rate-limited; retrying shortly.");
    } else if (overloaded || (typeof status === "number" && status >= 500)) {
      description = zh ? "服务繁忙，稍后自动重试。" : "The service is busy. Retrying automatically shortly.";
    } else {
      description = zh ? "连接暂时中断，恢复后会自动继续。" : "Connection interrupted. Your task will continue automatically once restored.";
    }
  } else if (stopped) {
    description = zh ? "当前进度已保留，等待你继续。" : "Your progress is saved. Continue when you are ready.";
  } else if (reason === "quota") {
    const reset = recovery.diagnostics?.reset_at;
    const seconds = typeof reset === "number" && Number.isFinite(reset) ? Math.max(0, Math.ceil(reset - Date.now() / 1000)) : null;
    description = seconds !== null
      ? (zh ? `模型服务额度不足，约 ${seconds} 秒后恢复。` : `Model service quota exhausted; expected to recover in about ${seconds}s.`)
      : (zh ? "模型服务额度不足。请检查余额或额度设置后继续。" : "Model service quota exhausted. Check your balance or quota settings before continuing.");
  } else if (reason === "region_blocked") {
    description = zh ? "该线路在当前地区被拦截，请换线路" : "This route is blocked in your region. Choose another route.";
  } else if (reason === "context_overflow") {
    description = zh ? "请求内容超过模型上下文长度限制。请缩短内容或切换更大上下文模型后继续。" : "The request exceeded the model's context length. Shorten the content or switch to a larger-context model.";
  } else if (reason === "tls_certificate" || reason === "tls") {
    description = zh ? "安全连接校验未通过。请检查证书或代理设置后继续。" : "Secure connection check failed. Check certificate or proxy settings before continuing.";
  } else if (reason === "dns_not_found") {
    description = zh ? "无法解析服务地址。请检查模型地址或网络设置后继续。" : "Could not resolve the service address. Check the model URL or network settings before continuing.";
  } else if (reason === "auth_rejected" || status === 401 || status === 403) {
    description = zh ? "服务拒绝了访问。请检查 API 密钥和访问权限后继续。" : "Access was refused by the service. Check your API key and permissions before continuing.";
  } else if (typeof status === "number" && status >= 300 && status < 400) {
    description = zh ? "服务地址发生跳转，请填写最终地址后继续。" : "The service URL redirected. Configure the final endpoint URL before continuing.";
  } else if (status === 429) {
    description = retryAfterSecs !== null
      ? (zh ? `请求太频繁，预计约 ${retryAfterSecs} 秒后恢复。当前进度已保留，等待你继续。` : `Too many requests; expected to recover in about ${retryAfterSecs}s. Your progress is saved.`)
      : (zh ? "请求太频繁。当前进度已保留，请稍后继续。" : "Too many requests. Your progress is saved; continue shortly.");
  } else if (reason === "idle_timeout" || reason === "timeout") {
    description = zh ? `模型长时间没有响应（已自动重试 ${recovery.retry_count} 次）。当前进度已保留，等待你继续。` : `The model did not respond in time (retried ${recovery.retry_count} times). Your progress is saved.`;
  } else if (overloaded || (typeof status === "number" && status >= 500)) {
    description = zh ? `服务繁忙，稍后自动重试或手动继续（已自动重试 ${recovery.retry_count} 次）。` : `The service is busy (retried ${recovery.retry_count} times). Your progress is saved.`;
  } else if (blocked) {
    description = zh ? "服务暂时无法接受请求。请检查模型服务配置后继续。" : "The service could not accept the request. Check its credentials, address and connection settings before continuing.";
  } else {
    description = zh ? `已自动重试 ${recovery.retry_count} 次。当前进度已保留，等待你继续。` : `Retried ${recovery.retry_count} times. Your progress is saved.`;
  }
  const Icon = retrying ? LoaderCircle : stopped ? Pause : WifiOff;
  return <section className={`ra-connection ${retrying ? "is-retrying" : stopped ? "is-stopped" : "is-disconnected"}`} aria-label={zh ? "模型连接状态" : "Model connection status"}>
    <Icon size={16} className={retrying ? "ra-spinner" : undefined} aria-hidden="true" />
    <div className="ra-connection-body">
      <div role="status" aria-live="polite">
        <div className="ra-connection-heading"><strong>{retrying ? (zh ? `正在重新连接 ${modelName}` : `Reconnecting to ${modelName}`) : stopped ? (zh ? "已停止重连" : "Reconnection stopped") : (zh ? `暂时无法连接 ${modelName}` : `Unable to connect to ${modelName}`)}</strong>
          {retrying && recovery.retry_count > 0 && <small>{recovery.max_retries === null
            ? (zh ? `重试 ${recovery.retry_count} · 等待网络恢复` : `Retry ${recovery.retry_count} · Waiting for network`)
            : `${zh ? "重试" : "Retry"} ${recovery.retry_count} / ${recovery.max_retries}`}</small>}</div>
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
