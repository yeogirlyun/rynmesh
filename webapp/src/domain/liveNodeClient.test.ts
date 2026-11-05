import { afterEach, expect, it, vi } from "vitest";
import { DEFAULT_READ_TIMEOUT_MS, makeLiveNodeClient } from "./liveNodeClient";
import { NodeClientError } from "./nodeClient";
import { digestApi } from "./digestClient";

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

/** fetch that never answers but honours its signal, like a request queued behind a full pool. */
function hangingFetch() {
  return vi.fn((_url: string, init?: RequestInit) => new Promise<Response>((_, reject) => {
    init?.signal?.addEventListener("abort", () => reject(init.signal!.reason), { once: true });
  }));
}

it("ends an unbounded GET after the default read timeout with status 0", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  const fetch = hangingFetch();
  vi.stubGlobal("fetch", fetch);
  const checked = expect(makeLiveNodeClient("/api/local").getSettings()).rejects.toMatchObject({ status: 0 });
  await vi.advanceTimersByTimeAsync(DEFAULT_READ_TIMEOUT_MS - 1);
  expect((fetch.mock.calls[0][1] as RequestInit).signal!.aborted).toBe(false);
  await vi.advanceTimersByTimeAsync(1);
  await checked;
  expect(vi.getTimerCount()).toBe(0);
});

it("cancels a GET through the caller's signal without the default timeout", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  const fetch = hangingFetch();
  vi.stubGlobal("fetch", fetch);
  const controller = new AbortController();
  const pending = makeLiveNodeClient("/api/local").listPeers({ quick: true }, { signal: controller.signal });
  const checked = expect(pending).rejects.toBeDefined();
  expect(vi.getTimerCount()).toBe(0);
  controller.abort();
  await checked;
  expect(fetch.mock.calls[0][0]).toBe("/api/local/peers?quick=true");
});

it("keeps the signal out of the device-list query string", async () => {
  const fetch = vi.fn(async () => Response.json([]));
  vi.stubGlobal("fetch", fetch);
  const client = makeLiveNodeClient("/api/local");
  await client.listPeers(undefined, { signal: new AbortController().signal });
  await client.directory!({ quick: true, signal: new AbortController().signal });
  await client.spaceStatus({ signal: new AbortController().signal }).catch(() => undefined);
  const urls = (fetch.mock.calls as unknown as Array<[string]>).map(([url]) => url);
  expect(urls).toEqual(["/api/local/peers", "/api/local/directory?quick=1", "/api/local/space"]);
});

it("never puts a default timeout on a mutation", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  const fetch = hangingFetch();
  vi.stubGlobal("fetch", fetch);
  void makeLiveNodeClient("/api/local").discoverPeers().catch(() => undefined);
  expect((fetch.mock.calls[0][1] as RequestInit).method).toBe("POST");
  expect((fetch.mock.calls[0][1] as RequestInit).signal ?? null).toBeNull();
  await vi.advanceTimersByTimeAsync(DEFAULT_READ_TIMEOUT_MS * 2);
  expect(vi.getTimerCount()).toBe(0);
});

it("reports a timed-out GET as a NodeClientError", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  vi.stubGlobal("fetch", hangingFetch());
  const failure = makeLiveNodeClient("/api/local").getSettings().catch((error: unknown) => error);
  await vi.advanceTimersByTimeAsync(DEFAULT_READ_TIMEOUT_MS);
  expect(await failure).toBeInstanceOf(NodeClientError);
});

it("bounds the discovery-status read and honours its signal", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  const fetch = hangingFetch();
  vi.stubGlobal("fetch", fetch);
  const unbounded = digestApi.getDiscoveryStatus().catch((error: { status?: number }) => error.status);
  await vi.advanceTimersByTimeAsync(DEFAULT_READ_TIMEOUT_MS);
  expect(await unbounded).toBe(0);
  const controller = new AbortController();
  const bounded = digestApi.getDiscoveryStatus({ signal: controller.signal }).catch(() => "aborted");
  controller.abort();
  expect(await bounded).toBe("aborted");
});
