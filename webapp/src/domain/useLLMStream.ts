import { useEffect, useState } from "react";
import { nodeControlUrl } from "./nodeUrl";

const MAX_OUTPUT_BYTES = 128 * 1024;
type StreamView = { taskId: string; text: string; interrupted: boolean };

/** A transient preview. The node-owned conversation remains the saved answer. */
export function useLLMStream(taskId: string | undefined) {
  const [view, setView] = useState<StreamView>({ taskId: "", text: "", interrupted: false });
  useEffect(() => {
    if (!taskId || typeof EventSource === "undefined") return;
    let active = true;
    let source: EventSource | undefined;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let idleTimer: ReturnType<typeof setTimeout> | undefined;
    let retries = 0;
    let sequence = -1;
    let text = "";
    const publish = (interrupted = false) => {
      if (active) setView({ taskId, text, interrupted });
    };
    publish();
    const failed = () => {
      source?.close();
      source = undefined;
      clearTimeout(idleTimer);
      clearTimeout(retryTimer);
      if (!active) return;
      publish(true);
      // Reconnect to the same order only. Never submit or cancel work here.
      if (retries++ < 3) retryTimer = setTimeout(connect, 1000);
    };
    const connect = () => {
      if (!active) return;
      try {
        source = new EventSource(nodeControlUrl(`/llm/orders/${encodeURIComponent(taskId)}/events?after_sequence=${sequence}`), { withCredentials: true });
      } catch { failed(); return; }
      const current = source;
      const heartbeat = () => {
        if (!active || source !== current) return;
        clearTimeout(idleTimer);
        idleTimer = setTimeout(failed, 45_000);
      };
      heartbeat();
      current.addEventListener("heartbeat", heartbeat);
      source.addEventListener("delta", (message) => {
        if (!active || source !== current) return;
        heartbeat();
        try {
          const raw = (message as MessageEvent<string>).data;
          if (raw.length > MAX_OUTPUT_BYTES * 6 + 1024) throw new Error("event limit");
          const event = JSON.parse(raw) as { sequence: number; delta: string; snapshot?: boolean };
          if (!Number.isSafeInteger(event.sequence) || event.sequence < 0 || typeof event.delta !== "string") throw new Error("invalid event");
          if (event.sequence <= sequence) return;
          if (event.snapshot !== true && event.sequence !== sequence + 1) throw new Error("sequence gap");
          const next = event.snapshot === true ? event.delta : text + event.delta;
          if (new TextEncoder().encode(next).length > MAX_OUTPUT_BYTES) throw new Error("output limit");
          text = next;
          sequence = event.sequence;
          publish();
        } catch { failed(); }
      });
      source.addEventListener("complete", () => {
        if (source !== current) return;
        current.close(); source = undefined; clearTimeout(idleTimer);
      });
      // Named server errors and connection failures both retain history polling.
      source.onerror = () => { if (source === current) failed(); };
    };
    connect();
    return () => { active = false; source?.close(); clearTimeout(retryTimer); clearTimeout(idleTimer); };
  }, [taskId]);
  return view.taskId === taskId ? view : { taskId: taskId ?? "", text: "", interrupted: false };
}
