import { isTauriDesktop, localControlHeaders } from "./nodeUrl";

/** Native EventSource cannot send the desktop launch header. Browser sessions
 * keep native SSE; desktop streams use fetch with the same header as JSON. */
export function localEventSource(url: string, options?: EventSourceInit): EventSource {
  if (!isTauriDesktop()) return new EventSource(url, options);
  class DesktopEvents extends EventTarget {
    onmessage: ((event: MessageEvent) => void) | null = null;
    onopen: ((event: Event) => void) | null = null;
    onerror: (() => void) | null = null;
    private controller = new AbortController();
    private timer: ReturnType<typeof setTimeout> | undefined;
    private closed = false;
    private lastId = "";
    constructor() { super(); void this.connect(); }
    close() { this.closed = true; this.controller.abort(); clearTimeout(this.timer); }
    private async connect() {
      try {
        const response = await fetch(url, {
          credentials: "include", cache: "no-store", signal: this.controller.signal,
          headers: { ...await localControlHeaders(), ...(this.lastId ? { "Last-Event-ID": this.lastId } : {}) },
        });
        if (!response.ok || !response.body) throw new Error("stream_unavailable");
        const opened = new Event("open");
        this.dispatchEvent(opened);
        this.onopen?.(opened);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let pending = "", event = "message", data: string[] = [];
        while (!this.closed) {
          const chunk = await reader.read();
          if (chunk.done) break;
          pending += decoder.decode(chunk.value, { stream: true });
          let newline: number;
          while ((newline = pending.indexOf("\n")) >= 0) {
            const line = pending.slice(0, newline).replace(/\r$/, "");
            pending = pending.slice(newline + 1);
            if (line === "") {
              if (data.length) {
                const message = new MessageEvent(event, { data: data.join("\n"), lastEventId: this.lastId });
                this.dispatchEvent(message);
                if (event === "message") this.onmessage?.(message);
              }
              data = []; event = "message";
            } else if (!line.startsWith(":")) {
              const colon = line.indexOf(":");
              const field = colon < 0 ? line : line.slice(0, colon);
              const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
              if (field === "data") data.push(value);
              else if (field === "event") event = value;
              else if (field === "id") this.lastId = value;
            }
          }
        }
      } catch {
        // Callers own the status message and may close/reconnect themselves.
      }
      if (!this.closed) {
        this.onerror?.();
        if (!this.closed) this.timer = setTimeout(() => void this.connect(), 1000);
      }
    }
  }
  return new DesktopEvents() as unknown as EventSource;
}
