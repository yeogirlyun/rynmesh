import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useLLMStream } from "./useLLMStream";

class Source extends EventTarget {
  static instances: Source[] = [];
  onerror: (() => void) | null = null;
  close = vi.fn();
  constructor(readonly url: string, readonly options: unknown) { super(); Source.instances.push(this); }
  emit(kind: string, value: unknown) { this.dispatchEvent(new MessageEvent(kind, { data: JSON.stringify(value) })); }
}
beforeEach(() => { Source.instances = []; vi.stubGlobal("EventSource", Source); vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] }); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

it("renders ordered deltas and reconnects to the same task without duplicating text", () => {
  const { result, unmount } = renderHook(() => useLLMStream("task_original"));
  const first = Source.instances[0];
  expect(first.options).toEqual({ withCredentials: true });
  act(() => first.emit("delta", { sequence: 0, delta: "Hello " }));
  act(() => first.emit("delta", { sequence: 1, delta: "世界" }));
  expect(result.current.text).toBe("Hello 世界");
  act(() => first.onerror?.());
  expect(result.current.interrupted).toBe(true);
  act(() => vi.advanceTimersByTime(1000));
  const second = Source.instances[1];
  expect(second.url).toContain("task_original/events?after_sequence=1");
  act(() => second.emit("delta", { sequence: 1, delta: "世界" }));
  expect(result.current.text).toBe("Hello 世界");
  act(() => second.emit("delta", { sequence: 4, delta: "Recovered whole preview", snapshot: true }));
  expect(result.current.text).toBe("Recovered whole preview");
  expect(result.current.interrupted).toBe(false);
  act(() => second.emit("complete", { state: "succeeded" }));
  expect(second.close).toHaveBeenCalled();
  unmount();
});

it("bounds retries and output, and does not mix previews across conversations", () => {
  const { result, rerender, unmount } = renderHook(({ task }) => useLLMStream(task), { initialProps: { task: "first" } });
  act(() => Source.instances[0].emit("delta", { sequence: 0, delta: "private preview" }));
  rerender({ task: "second" });
  expect(result.current.text).toBe("");
  expect(Source.instances[0].close).toHaveBeenCalled();
  act(() => Source.instances[0].emit("delta", { sequence: 1, delta: "late first answer" }));
  expect(result.current.text).toBe("");
  act(() => Source.instances[1].emit("delta", { sequence: 0, delta: "x".repeat(128 * 1024 + 1) }));
  expect(result.current.text).toBe("");
  for (let i = 0; i < 4; i++) {
    act(() => vi.advanceTimersByTime(1000));
    act(() => Source.instances.at(-1)?.onerror?.());
  }
  expect(Source.instances).toHaveLength(5); // First task + four attempts for second.
  unmount();
  act(() => vi.advanceTimersByTime(5000));
  expect(Source.instances).toHaveLength(5);
});

it("fails a sequence gap visibly while preserving the last valid preview", () => {
  const { result } = renderHook(() => useLLMStream("original"));
  act(() => Source.instances[0].emit("delta", { sequence: 0, delta: "valid" }));
  act(() => Source.instances[0].emit("delta", { sequence: 2, delta: "gap" }));
  expect(result.current.text).toBe("valid");
  expect(result.current.interrupted).toBe(true);
});
