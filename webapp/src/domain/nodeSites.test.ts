import { afterEach, expect, it, vi } from "vitest";
import { nodeSites } from "./nodeSites";

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

it.each(["headers", "body"])("releases a stalled status %s request and permits recovery", async phase => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const stalled = new Promise<never>(() => {});
  const fetcher = vi.fn().mockImplementationOnce(() => phase === "headers" ? stalled
    : Promise.resolve({ ok: true, json: () => stalled }));
  vi.stubGlobal("fetch", fetcher);
  const pending = nodeSites.status("test", true);
  const rejected = expect(pending).rejects.toMatchObject({ code: "site_unavailable", status: 0 });
  await vi.advanceTimersByTimeAsync(6_000);
  await rejected;
  expect(fetcher.mock.calls[0][1].signal.aborted).toBe(true);
  fetcher.mockResolvedValueOnce({ ok: true, json: async () => ({ online: true, route: "lan" }) });
  await expect(nodeSites.status("test", true)).resolves.toMatchObject({ online: true });
  expect(vi.getTimerCount()).toBe(0);
});
