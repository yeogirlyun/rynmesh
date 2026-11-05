import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { isPollTimeout, startPoller, type Poller } from "./polling";

let poller: Poller | null = null;

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}

/** A run that stays pending until its signal aborts or `finish` is called. */
function controllableRun() {
  let inFlight = 0;
  let maxInFlight = 0;
  let calls = 0;
  const signals: AbortSignal[] = [];
  const finishers: Array<() => void> = [];
  const run = vi.fn((signal: AbortSignal) => {
    calls += 1;
    inFlight += 1;
    maxInFlight = Math.max(maxInFlight, inFlight);
    signals.push(signal);
    return new Promise<void>((resolve, reject) => {
      const done = (error?: unknown) => { inFlight -= 1; if (error) reject(error); else resolve(); };
      finishers.push(() => done());
      signal.addEventListener("abort", () => done(signal.reason), { once: true });
    });
  });
  return { run, signals, finishers, stats: () => ({ calls, inFlight, maxInFlight }) };
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  setVisibility("visible");
});

afterEach(() => {
  poller?.stop();
  poller = null;
  vi.useRealTimers();
  setVisibility("visible");
});

it("never overlaps runs when a run takes longer than the interval", async () => {
  const { run, finishers, stats } = controllableRun();
  poller = startPoller({ run, intervalMs: 2000, timeoutMs: 120_000 });
  await vi.advanceTimersByTimeAsync(60_000);
  expect(stats()).toMatchObject({ calls: 1, inFlight: 1, maxInFlight: 1 });
  finishers[0]();
  await vi.advanceTimersByTimeAsync(1999);
  expect(stats().calls).toBe(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(stats()).toMatchObject({ calls: 2, maxInFlight: 1 });
});

it("aborts a run at its timeout and schedules the next one with backoff", async () => {
  const { run, signals, stats } = controllableRun();
  poller = startPoller({ run, intervalMs: 2000, timeoutMs: 5000 });
  await vi.advanceTimersByTimeAsync(4999);
  expect(signals[0].aborted).toBe(false);
  await vi.advanceTimersByTimeAsync(1);
  expect(signals[0].aborted).toBe(true);
  expect(isPollTimeout(signals[0].reason)).toBe(true);
  // First failure: one interval. Second run starts 2 s later and also times out.
  await vi.advanceTimersByTimeAsync(2000);
  expect(stats().calls).toBe(2);
  await vi.advanceTimersByTimeAsync(5000);
  // Second consecutive failure doubles the delay.
  await vi.advanceTimersByTimeAsync(3999);
  expect(stats().calls).toBe(2);
  await vi.advanceTimersByTimeAsync(1);
  expect(stats().calls).toBe(3);
});

it("caps the failure backoff and resets it after a success", async () => {
  let fail = true;
  const run = vi.fn(async () => { if (fail) throw new Error("down"); });
  poller = startPoller({ run, intervalMs: 1000, timeoutMs: 500, maxBackoffMs: 4000 });
  await vi.advanceTimersByTimeAsync(0); // run 1 fails
  await vi.advanceTimersByTimeAsync(1000); // run 2 (1 s)
  await vi.advanceTimersByTimeAsync(2000); // run 3 (2 s)
  await vi.advanceTimersByTimeAsync(4000); // run 4 (4 s)
  await vi.advanceTimersByTimeAsync(4000); // run 5 (capped at 4 s, not 8 s)
  expect(run).toHaveBeenCalledTimes(5);
  fail = false;
  await vi.advanceTimersByTimeAsync(4000); // run 6 succeeds
  expect(run).toHaveBeenCalledTimes(6);
  await vi.advanceTimersByTimeAsync(1000); // back to the plain interval
  expect(run).toHaveBeenCalledTimes(7);
});

it("uses the hidden interval and runs at once when the window becomes visible", async () => {
  const run = vi.fn(async () => undefined);
  poller = startPoller({ run, intervalMs: 5000, hiddenIntervalMs: 30_000, timeoutMs: 1000 });
  await vi.advanceTimersByTimeAsync(0);
  expect(run).toHaveBeenCalledTimes(1);
  setVisibility("hidden");
  await vi.advanceTimersByTimeAsync(5000); // scheduled while visible
  expect(run).toHaveBeenCalledTimes(2);
  await vi.advanceTimersByTimeAsync(29_999);
  expect(run).toHaveBeenCalledTimes(2);
  await vi.advanceTimersByTimeAsync(1);
  expect(run).toHaveBeenCalledTimes(3);
  await vi.advanceTimersByTimeAsync(10_000);
  setVisibility("visible");
  expect(run).toHaveBeenCalledTimes(4);
});

it("does not start a second run when the window becomes visible during a run", async () => {
  const { run, stats } = controllableRun();
  poller = startPoller({ run, intervalMs: 5000, hiddenIntervalMs: 30_000, timeoutMs: 60_000 });
  setVisibility("hidden");
  await vi.advanceTimersByTimeAsync(20_000);
  setVisibility("visible");
  expect(stats()).toMatchObject({ calls: 1, maxInFlight: 1 });
});

it("collapses refresh calls made during a run into one extra run", async () => {
  const { run, finishers, stats } = controllableRun();
  poller = startPoller({ run, intervalMs: 10_000, timeoutMs: 60_000 });
  const waits = [poller.refresh(), poller.refresh(), poller.refresh()];
  let resolved = 0;
  for (const wait of waits) void wait.then(() => { resolved += 1; });
  finishers[0]();
  await vi.advanceTimersByTimeAsync(0);
  expect(stats()).toMatchObject({ calls: 2, maxInFlight: 1 });
  expect(resolved).toBe(0); // they wait for the extra run, not the one already in flight
  finishers[1]();
  await vi.advanceTimersByTimeAsync(0);
  expect(resolved).toBe(3);
  await vi.advanceTimersByTimeAsync(9_999);
  expect(stats().calls).toBe(2);
});

it("stops: aborts the in-flight run and never runs again", async () => {
  const { run, signals, stats } = controllableRun();
  poller = startPoller({ run, intervalMs: 1000, timeoutMs: 60_000 });
  const pending = poller.refresh();
  poller.stop();
  expect(signals[0].aborted).toBe(true);
  await expect(pending).resolves.toBeUndefined();
  await vi.advanceTimersByTimeAsync(60_000);
  setVisibility("hidden");
  setVisibility("visible");
  await expect(poller.refresh()).resolves.toBeUndefined();
  expect(stats().calls).toBe(1);
  expect(vi.getTimerCount()).toBe(0);
});

it("waits one interval first when immediate is false", async () => {
  const run = vi.fn(async () => undefined);
  poller = startPoller({ run, intervalMs: 3000, timeoutMs: 1000, immediate: false });
  await vi.advanceTimersByTimeAsync(2999);
  expect(run).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1);
  expect(run).toHaveBeenCalledTimes(1);
});
