import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { startPeerPolling } from "./peerPolling";
import type { Poller } from "./polling";
import type { Peer, PeerFilters } from "./types";

let poller: Poller | null = null;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
});
afterEach(() => {
  poller?.stop();
  poller = null;
  vi.useRealTimers();
});

function hangingClient() {
  let inFlight = 0;
  let maxInFlight = 0;
  const signals: AbortSignal[] = [];
  const listPeers = vi.fn((_filters?: PeerFilters, opts?: { signal?: AbortSignal }) => {
    inFlight += 1;
    maxInFlight = Math.max(maxInFlight, inFlight);
    const signal = opts!.signal!;
    signals.push(signal);
    return new Promise<Peer[]>((_, reject) => {
      signal.addEventListener("abort", () => { inFlight -= 1; reject(signal.reason); }, { once: true });
    });
  });
  return { listPeers, signals, stats: () => ({ inFlight, maxInFlight }) };
}

it("keeps at most one device-list request in flight even when every request hangs for a minute", async () => {
  const client = hangingClient();
  poller = startPeerPolling(client, () => undefined);
  await vi.advanceTimersByTimeAsync(60_000);
  expect(client.stats().maxInFlight).toBe(1);
  // The 2 s interval used to start 30 requests in this minute; now each hung one is bounded first.
  expect(client.listPeers.mock.calls.length).toBeLessThanOrEqual(4);
  poller.stop();
  expect(client.signals.every((signal) => signal.aborted)).toBe(true);
  expect(client.stats().inFlight).toBe(0);
});

it("starts with full discovery, then reads the quick list every 5 s and full discovery every 15 s", async () => {
  const listPeers = vi.fn(async (_filters?: PeerFilters, _opts?: { signal?: AbortSignal }) => [] as Peer[]);
  const applied: boolean[] = [];
  poller = startPeerPolling({ listPeers }, (_items, merge) => applied.push(merge));
  await vi.advanceTimersByTimeAsync(0);
  await vi.advanceTimersByTimeAsync(15_000);
  const kinds = listPeers.mock.calls.map(([filters]) => (filters?.quick ? "quick" : "full"));
  expect(kinds).toEqual(["full", "quick", "quick", "full"]);
  expect(applied).toEqual([false, true, true, false]);
  for (const [, opts] of listPeers.mock.calls as unknown as Array<[PeerFilters | undefined, { signal: AbortSignal }]>) {
    expect(opts.signal).toBeInstanceOf(AbortSignal);
  }
});

it("only reads the quick list while the window is hidden", async () => {
  const listPeers = vi.fn(async (_filters?: PeerFilters, _opts?: { signal?: AbortSignal }) => [] as Peer[]);
  poller = startPeerPolling({ listPeers }, () => undefined);
  await vi.advanceTimersByTimeAsync(0);
  Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
  await vi.advanceTimersByTimeAsync(5_000 + 30_000 * 3);
  const kinds = listPeers.mock.calls.map(([filters]) => (filters?.quick ? "quick" : "full"));
  expect(kinds[0]).toBe("full");
  expect(kinds.slice(1).every((kind) => kind === "quick")).toBe(true);
  expect(kinds.length).toBe(5);
});
