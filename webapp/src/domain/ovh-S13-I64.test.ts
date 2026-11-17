import { afterEach, expect, it, vi } from "vitest";
import { localEventSource } from "./localEventSource";

afterEach(() => {
  vi.useRealTimers(); vi.unstubAllGlobals();
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
});

it("T4 desktop SSE publishes open on initial connection and automatic reconnect", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: { invoke: vi.fn(async () => "launch-test") } });
  let second: ReadableStreamDefaultController<Uint8Array> | undefined;
  let count = 0;
  const fetcher = vi.fn(async () => {
    count += 1;
    return new Response(new ReadableStream<Uint8Array>({ start(controller) {
      if (count === 1) controller.close(); else second = controller;
    } }));
  });
  vi.stubGlobal("fetch", fetcher);
  const source = localEventSource("/api/local/messages/stream");
  const opened = vi.fn();
  source.onopen = opened;
  try {
    await vi.waitFor(() => expect(opened).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(opened).toHaveBeenCalledTimes(2);
  } finally { source.close(); second?.close(); }
});
