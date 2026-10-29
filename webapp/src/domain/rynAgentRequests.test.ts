import { afterEach, expect, it, vi } from "vitest";
import { rynAgent } from "./rynAgent";

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

it.each([503, "network"])("retries one transient GET failure (%s) and returns the recovered state", async failure => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const fetch = vi.fn().mockImplementationOnce(() => failure === "network"
    ? Promise.reject(new TypeError("Failed to fetch"))
    : Promise.resolve(Response.json({ detail: "agent_unavailable" }, { status: 503 })))
    .mockResolvedValueOnce(Response.json({ tasks: [{ id: "recovered" }] }));
  vi.stubGlobal("fetch", fetch);
  const pending = rynAgent.tasks();
  await vi.advanceTimersByTimeAsync(250);
  await expect(pending).resolves.toEqual({ tasks: [{ id: "recovered" }] });
  expect(fetch).toHaveBeenCalledTimes(2);
});

it("bounds a stalled response body, cancels it, and retries the GET once", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const fetch = vi.fn().mockResolvedValueOnce({ ok: true, json: () => new Promise(() => {}) })
    .mockResolvedValueOnce(Response.json({ tasks: [] }));
  vi.stubGlobal("fetch", fetch);
  const pending = rynAgent.tasks();
  await vi.advanceTimersByTimeAsync(10000);
  expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
  await vi.advanceTimersByTimeAsync(250);
  await expect(pending).resolves.toEqual({ tasks: [] });
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(vi.getTimerCount()).toBe(0);
});

it("ends two hung GET attempts with an explicit timeout instead of leaving polling pending", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const fetch = vi.fn(() => new Promise(() => {}));
  vi.stubGlobal("fetch", fetch);
  const checked = expect(rynAgent.tasks()).rejects.toMatchObject({ code: "agent_request_timeout", status: 0 });
  await vi.advanceTimersByTimeAsync(20250);
  await checked;
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(vi.getTimerCount()).toBe(0);
});

it("cancels a hung GET immediately when its view closes and does not retry it", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const fetch = vi.fn(() => new Promise(() => {}));
  vi.stubGlobal("fetch", fetch);
  const controller = new AbortController();
  const checked = expect(rynAgent.tasks(controller.signal)).rejects.toMatchObject({ name: "AbortError" });
  controller.abort();
  await checked;
  await vi.advanceTimersByTimeAsync(30000);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(vi.getTimerCount()).toBe(0);
});

it.each([401, 404, 409])("surfaces HTTP %s without automatic request retries", async status => {
  const fetch = vi.fn().mockResolvedValue(Response.json({ detail: "denied" }, { status }));
  vi.stubGlobal("fetch", fetch);
  await expect(rynAgent.tasks()).rejects.toMatchObject({ code: "denied", status });
  expect(fetch).toHaveBeenCalledTimes(1);
});

const mutations = [
  ["POST", () => rynAgent.create("Question", "model", "request-1")],
  ["PUT", () => rynAgent.updateSettings({ enabled: true })],
  ["PATCH", () => rynAgent.renameTask("task-1", "Title", 1, "request-2")],
  ["DELETE", () => rynAgent.removeProject("project-1")],
] as const;

it.each(mutations)("never replays a %s mutation after a server failure", async (method, run) => {
  const fetch = vi.fn().mockResolvedValue(Response.json({ detail: "agent_unavailable" }, { status: 503 }));
  vi.stubGlobal("fetch", fetch);
  await expect(run()).rejects.toMatchObject({ status: 503 });
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0][1].method).toBe(method);
});

it("never retries an uncertain mutation after a connection loss", async () => {
  const fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
  vi.stubGlobal("fetch", fetch);
  await expect(rynAgent.control("task-1", "messages", { message: "Follow up" })).rejects.toBeInstanceOf(TypeError);
  expect(fetch).toHaveBeenCalledTimes(1);
});
