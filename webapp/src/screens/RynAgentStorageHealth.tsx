import { useEffect, useState } from "react";
import { CircleAlert } from "lucide-react";
import { rynAgent } from "../domain/rynAgent";
import type { AgentStorageHealth } from "../domain/agentStorageHealth";

export default function RynAgentStorageHealth({ health, zh, onRefresh }: {
  health?: AgentStorageHealth; zh: boolean; onRefresh: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [openError, setOpenError] = useState(false);
  useEffect(() => {
    if (!health || !["damaged", "unavailable"].includes(health.state)) return;
    const timer = setInterval(onRefresh, 30000);
    return () => clearInterval(timer);
  }, [health?.state, onRefresh]);
  if (!health || health.state === "ok") return null;
  const text = health.damaged_tasks > 0
    ? zh ? `${health.damaged_tasks} 个任务记录无法读取，已隔离；其他任务可以继续。` : `${health.damaged_tasks} task records could not be read and were isolated. Other tasks can continue.`
    : health.state === "recovered"
      ? zh ? "Agent 历史已从上一份备份恢复。" : "Agent history was recovered from its previous backup."
      : zh ? "Agent 历史暂时无法读取，正在每 30 秒自动检查恢复。" : "Agent history cannot be read. Automatic recovery checks run every 30 seconds.";
  const open = async () => {
    setBusy(true); setOpenError(false);
    try { await rynAgent.openStorageBackups(); }
    catch { setOpenError(true); }
    finally { setBusy(false); }
  };
  return <div role="alert" className="ra-banner ra-storage-health"><CircleAlert size={17} /><span>{text}
    {openError && <span> {zh ? "无法打开备份目录，请稍后重试。" : "Could not open the backup directory. Please retry."}</span>}</span>
    <button type="button" disabled={busy || !health.quarantine_dir} onClick={() => void open()}>{zh ? "打开备份目录" : "Open backup directory"}</button>
  </div>;
}
