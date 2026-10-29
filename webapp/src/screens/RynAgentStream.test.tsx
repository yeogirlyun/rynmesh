import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import RynAgentStream from "./RynAgentStream";

vi.mock("./ChatMarkdown", () => ({ default: ({ children }: { children: string }) => <div data-testid="stream-text">{children}</div> }));

const shown = () => screen.getByTestId("stream-text").textContent || "";
const advance = (ms: number) => act(() => vi.advanceTimersByTime(ms));

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.stubGlobal("matchMedia", undefined);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("coalesces the initial character and smoothly catches up with a following burst within 180 ms", () => {
  const view = render(<RynAgentStream text="我" streamKey="run-1" />);
  advance(40);
  const text = "我先检查这台电脑的空间和磁盘状态，再查看常见占用位置。";
  view.rerender(<RynAgentStream text={text} streamKey="run-1" />);
  advance(39);
  expect(shown()).toBe("");
  advance(1);
  expect(shown().startsWith("我先检查")).toBe(true);
  expect(shown().length).toBeLessThan(text.length);
  advance(100);
  expect(shown()).toBe(text);
});

it("keeps the existing catch-up deadline when more text arrives", () => {
  const original = "a".repeat(120);
  const final = original + "b".repeat(240);
  const view = render(<RynAgentStream text={original} streamKey="run-1" />);
  advance(80);
  view.rerender(<RynAgentStream text={final} streamKey="run-1" />);
  advance(100);
  expect(shown()).toBe(final);
});

it("flushes completed output synchronously and cancels the presentation timer", () => {
  const text = "The complete response is ready.";
  const view = render(<RynAgentStream text="T" streamKey="run-1" />);
  view.rerender(<RynAgentStream text={text} streamKey="run-1" active={false} />);
  expect(shown()).toBe(text);
  expect(vi.getTimerCount()).toBe(0);
});

it("never shows text from a previous model run when the stream key changes", () => {
  const view = render(<RynAgentStream text="Previous output that is still arriving." streamKey="run-1" />);
  advance(80);
  expect(shown()).not.toBe("");
  view.rerender(<RynAgentStream text="新" streamKey="run-2" />);
  expect(shown()).toBe("");
  advance(180);
  expect(shown()).toBe("新");
});

it("preserves surrogate pairs in every paced frame and cancels timers on unmount", () => {
  const text = "检查😀🧑🚀结果".repeat(12);
  const view = render(<RynAgentStream text={text} streamKey="run-1" />);
  advance(80);
  for (let frame = 0; frame < 4; frame++) {
    const points = Array.from(shown());
    expect(points.some(point => /^[\uD800-\uDFFF]$/.test(point))).toBe(false);
    advance(32);
  }
  expect(shown()).toBe(text);
  view.rerender(<RynAgentStream text={`${text} More output.`} streamKey="run-1" />);
  expect(vi.getTimerCount()).toBeGreaterThan(0);
  view.unmount();
  expect(vi.getTimerCount()).toBe(0);
});

it("renders immediately when reduced motion is preferred", () => {
  const addEventListener = vi.fn(), removeEventListener = vi.fn();
  vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener, removeEventListener }));
  const text = "Show all output immediately.";
  const view = render(<RynAgentStream text={text} streamKey="run-1" />);
  expect(shown()).toBe(text);
  expect(vi.getTimerCount()).toBe(0);
  view.unmount();
  expect(removeEventListener).toHaveBeenCalledWith("change", expect.any(Function));
});

it("flushes a pending burst when the reduced-motion preference changes", () => {
  let change = () => {};
  const media = { matches: false, addEventListener: (_event: string, listener: () => void) => { change = listener; }, removeEventListener: vi.fn() };
  vi.stubGlobal("matchMedia", () => media);
  const text = "A response awaiting its first buffered frame.";
  render(<RynAgentStream text={text} streamKey="run-1" />);
  expect(shown()).toBe("");
  act(() => { media.matches = true; change(); });
  expect(shown()).toBe(text);
  expect(vi.getTimerCount()).toBe(0);
});

it("shows corrected or shortened snapshots immediately without stale buffered text", () => {
  const view = render(<RynAgentStream text="An initial answer still arriving." streamKey="run-1" />);
  advance(80);
  view.rerender(<RynAgentStream text="A corrected answer." streamKey="run-1" />);
  expect(shown()).toBe("A corrected answer.");
  expect(vi.getTimerCount()).toBe(0);
});
