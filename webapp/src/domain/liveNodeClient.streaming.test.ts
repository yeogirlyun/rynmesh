import { afterEach, describe, expect, it, vi } from "vitest";
import { makeLiveNodeClient } from "./liveNodeClient";

class FakeEventSource {
  static latest: FakeEventSource | null = null;
  readonly url: string;
  readonly withCredentials: boolean;
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  private listeners = new Map<string, Array<(event: MessageEvent<string>) => void>>();

  constructor(url: string | URL, init?: EventSourceInit) {
    this.url = String(url);
    this.withCredentials = Boolean(init?.withCredentials);
    FakeEventSource.latest = this;
  }

  addEventListener(name: string, listener: EventListenerOrEventListenerObject) {
    const callback = listener as (event: MessageEvent<string>) => void;
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), callback]);
  }

  emit(name: string, value: unknown) {
    const event = new MessageEvent(name, { data: JSON.stringify(value) });
    for (const listener of this.listeners.get(name) ?? []) listener(event);
  }

  close() {
    this.closed = true;
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
  FakeEventSource.latest = null;
});

describe("live Private AI SSE client", () => {
  it("subscribes only to the local node, resumes after sequence, and closes cleanly", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const onEvent = vi.fn();
    const onDisconnect = vi.fn();
    const close = makeLiveNodeClient("/api/local").subscribeLLMOrder(
      "task / stream",
      { onEvent, onDisconnect },
      7,
    );
    const source = FakeEventSource.latest!;
    expect(source.url).toBe("/api/local/llm/orders/task%20%2F%20stream/events?after_sequence=7");
    expect(source.withCredentials).toBe(true);

    source.emit("delta", { event: "forged", sequence: 8, delta: "verified local plaintext" });
    expect(onEvent).toHaveBeenCalledWith({
      event: "delta", sequence: 8, delta: "verified local plaintext",
    });
    source.onerror?.(new Event("error"));
    expect(onDisconnect).toHaveBeenCalledOnce();

    close();
    expect(source.closed).toBe(true);
    source.onerror?.(new Event("error"));
    expect(onDisconnect).toHaveBeenCalledOnce();
  });
});
