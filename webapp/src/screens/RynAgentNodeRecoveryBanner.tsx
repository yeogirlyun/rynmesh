import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { getDesktopStatus, runDesktopAction, type DesktopStatus } from "../domain/desktopClient";
import { isTauriDesktop } from "../domain/nodeUrl";

export default function RynAgentNodeRecoveryBanner() {
  const { i18n } = useTranslation();
  const zh = i18n.language.startsWith("zh");
  const [status, setStatus] = useState<DesktopStatus | null>(null);
  const [openingLogs, setOpeningLogs] = useState(false);

  useEffect(() => {
    if (!isTauriDesktop()) return;
    let active = true;
    const poll = () => {
      getDesktopStatus()
        .then(next => {
          if (active) setStatus(next);
        })
        .catch(() => {
          if (active) setStatus(null);
        });
    };
    poll();
    const timer = setInterval(poll, 3000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);

  if (!status || status.recovery !== "needs_attention") return null;

  const openLogs = async () => {
    if (openingLogs) return;
    setOpeningLogs(true);
    try {
      await runDesktopAction("logs");
    } catch {
      // Desktop shell handles error translation; keep banner interactive.
    } finally {
      setOpeningLogs(false);
    }
  };

  return (
    <div className="ra-banner ra-node-recovery-banner" role="alert">
      <span>
        {zh
          ? "Ryn 节点无法启动，正在定期重试。"
          : "Ryn node could not start and is retrying periodically."}
        {status.last_error && (
          <>
            {" "}
            <code>{status.last_error}</code>
          </>
        )}
      </span>
      <button type="button" disabled={openingLogs} onClick={() => void openLogs()}>
        {zh ? "打开日志" : "Open logs"}
      </button>
    </div>
  );
}
