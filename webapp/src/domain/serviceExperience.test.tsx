import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useProviderDiscovery, useServiceOrder } from "./serviceExperience";
import { providerIdentity } from "./serviceDescriptors";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const tick = (ms = 0) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

describe("service lifecycle contracts", () => {
  beforeEach(() => vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] }));
  afterEach(() => vi.useRealTimers());

  it("waits for completion before scheduling, and coalesces manual refreshes", async () => {
    const slow = deferred<number>();
    const load = vi.fn().mockReturnValueOnce(slow.promise).mockResolvedValue(2);
    const scope = {};
    const { result } = renderHook(() => useServiceOrder({ scope, key: "order", load, intervalMs: 1000 }));
    await tick(10_000);
    expect(load).toHaveBeenCalledTimes(1);
    let first!: Promise<unknown>; let second!: Promise<unknown>;
    act(() => { first = result.current.refresh(); second = result.current.refresh(); });
    expect(first).toBe(second);
    await act(async () => { slow.resolve(1); await first; });
    await tick(999); expect(load).toHaveBeenCalledTimes(1);
    await tick(1); expect(load).toHaveBeenCalledTimes(2);
    expect(result.current.data).toBe(2);
  });

  it("rejects late results after network, provider, or client changes", async () => {
    const old = deferred<string>(); const scope = {}; const replacement = {};
    const seen = vi.fn();
    const { result, rerender } = renderHook(({ key, client }) => useServiceOrder({ scope: client, key,
      load: () => client === scope ? old.promise : Promise.resolve("new client"), intervalMs: 1000, onData: seen }),
    { initialProps: { key: "network-a/peer-a/order", client: scope } });
    await tick();
    rerender({ key: "network-b/peer-b/order", client: replacement });
    expect(result.current.data).toBeUndefined();
    await tick();
    await act(async () => { old.resolve("old response"); await old.promise; });
    expect(result.current.data).toBe("new client");
    expect(seen).not.toHaveBeenCalledWith("old response");
  });

  it("stops terminal polling, but permits an explicit reconciliation read", async () => {
    const load = vi.fn().mockResolvedValue({ done: true }); const scope = {};
    const { result } = renderHook(() => useServiceOrder<{ done: boolean }>({ scope, key: "order", load, intervalMs: 1000, isTerminal: (row) => row.done }));
    await tick(60_000); expect(load).toHaveBeenCalledTimes(1);
    await act(async () => { await result.current.refresh(); });
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("backs off failed reads and resumes the ordinary cadence after recovery", async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error("offline")).mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue("online"); const scope = {};
    const { result } = renderHook(() => useServiceOrder({ scope, key: "order", load, intervalMs: 1000 }));
    await tick(); expect(result.current.error?.message).toBe("offline");
    await tick(1999); expect(load).toHaveBeenCalledTimes(1);
    await tick(1); expect(load).toHaveBeenCalledTimes(2);
    await tick(3999); expect(load).toHaveBeenCalledTimes(2);
    await tick(1); expect(result.current.data).toBe("online");
    await tick(1000); expect(load).toHaveBeenCalledTimes(4);
    expect(result.current.error).toBeNull();
  });

  it("serializes explicit operations, discards pre-operation reads and never retries a failed write", async () => {
    const read = deferred<string>(); const write = deferred<string>(); const scope = {};
    const load = vi.fn().mockReturnValueOnce(read.promise).mockResolvedValue("reconciled");
    const operation = vi.fn(() => write.promise); const seen = vi.fn();
    const { result } = renderHook(() => useServiceOrder({ scope, key: "route", load, intervalMs: 1000, onData: seen }));
    await tick();
    let pending!: Promise<string>;
    act(() => { pending = result.current.execute(operation, (value) => value); });
    await expect(result.current.execute(operation)).rejects.toThrow("Another operation");
    await act(async () => { read.resolve("disconnected before write"); await read.promise; });
    expect(seen).not.toHaveBeenCalledWith("disconnected before write");
    await act(async () => { write.resolve("connected"); await pending; });
    expect(result.current.data).toBe("connected");
    await tick(); expect(result.current.data).toBe("reconciled");
    const failed = vi.fn().mockRejectedValue(new Error("lost response"));
    await act(async () => { await expect(result.current.execute(failed)).rejects.toThrow("lost response"); });
    await tick(10_000); expect(failed).toHaveBeenCalledTimes(1);
    expect(operation).toHaveBeenCalledTimes(1);
  });

  it("stops local polling on unmount without cancelling an accepted remote operation", async () => {
    const write = deferred<string>(); const operation = vi.fn(() => write.promise); const scope = {};
    const load = vi.fn().mockResolvedValue("ready"); const onData = vi.fn();
    const { result, unmount } = renderHook(() => useServiceOrder({ scope, key: "order", load, onData, intervalMs: 1000 }));
    await tick();
    let pending!: Promise<string>;
    act(() => { pending = result.current.execute(operation, (value) => value); });
    unmount();
    write.resolve("accepted"); expect(await pending).toBe("accepted");
    await tick(10_000); expect(load).toHaveBeenCalledTimes(1);
    expect(onData).not.toHaveBeenCalledWith("accepted");
  });

  it("deduplicates identical service identities while preserving different networks and packages", async () => {
    const scope = {}; const rows = [
      ["net-a", "peer", "model-a"], ["net-a", "peer", "model-a"],
      ["net-b", "peer", "model-a"], ["net-a", "peer", "model-b"],
    ];
    const { result } = renderHook(() => useProviderDiscovery({ scope, key: "catalog", intervalMs: 1000,
      load: async () => rows, identity: (row) => providerIdentity(row[0], row[1], row[2]) }));
    await tick(); expect(result.current.providers).toHaveLength(3);
  });

  it("awaits the original fixture task and stops its local wait when the view closes", async () => {
    const scope = {}; const read = vi.fn().mockResolvedValue({ done: true });
    const { result, unmount } = renderHook(() => useServiceOrder<{ done: boolean }>({ scope, key: "fixture",
      enabled: false, load: read, intervalMs: 1000, isTerminal: (row) => row.done }));
    let first!: Promise<{ done: boolean }>;
    act(() => { first = result.current.awaitTerminal({ done: false }, read); });
    await tick(999); expect(read).not.toHaveBeenCalled();
    await tick(1); expect(await first).toEqual({ done: true });
    let pending!: Promise<unknown>;
    act(() => { pending = result.current.awaitTerminal({ done: false }, read).catch((error) => error.message); });
    unmount();
    expect(await pending).toContain("task continues on the node");
    await tick(5000); expect(read).toHaveBeenCalledTimes(1);
  });

  it("keeps an operation serialized across a client change and refreshes the new client afterwards", async () => {
    const write = deferred<string>(); const oldScope = {}; const newScope = {};
    const read = vi.fn().mockResolvedValue("current");
    const { result, rerender } = renderHook(({ scope }) => useServiceOrder({ scope, key: "route",
      load: read, intervalMs: 1000 }), { initialProps: { scope: oldScope } });
    await tick();
    let pending!: Promise<string>;
    act(() => { pending = result.current.execute(() => write.promise); });
    rerender({ scope: newScope });
    await expect(result.current.execute(async () => "duplicate")).rejects.toThrow("Another operation");
    await act(async () => { write.resolve("old operation done"); await pending; });
    await tick(); expect(result.current.busy).toBe(false);
    expect(result.current.data).toBe("current");
  });
});
