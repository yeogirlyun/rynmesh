import { afterEach, expect, it, vi } from "vitest";
import { initializeDesktopConnection } from "./nodeUrl";
import { localEventSource } from "./localEventSource";
import { uploadNativeModel } from "./nativeModel";
import { makeLiveNodeClient } from "./liveNodeClient";
import { rynAgent } from "./rynAgent";

afterEach(() => { vi.unstubAllGlobals(); delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__; });

it.each([true, false])("T5 desktop=%s carries launch credential in both real request wrappers", async desktop => {
  if (desktop) Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: { invoke: vi.fn(async (name: string) => name === "local_token" ? "launch-token" : 8791) } });
  else delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  const fetcher = vi.fn(async () => Response.json({}));
  vi.stubGlobal("fetch", fetcher);
  await makeLiveNodeClient("http://localhost/api/local").getNodeStatus();
  await rynAgent.projects();
  for (const [, init] of fetcher.mock.calls as unknown as [string, RequestInit][]) {
    expect(new Headers(init.headers).get("X-Ryn-Local-Token")).toBe(desktop ? "launch-token" : null);
    expect(init.credentials).toBe("include");
  }
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
});

it("desktop native-model XHR upload carries the same launch header", async () => {
  Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: { invoke: vi.fn(async () => "launch-token") } });
  const setHeader = vi.fn();
  class Upload {
    open() {}
    upload = {};
    withCredentials = false;
    status = 200;
    responseText = "{}";
    onload?: () => void;
    setRequestHeader = setHeader;
    send() { this.onload?.(); }
  }
  vi.stubGlobal("XMLHttpRequest", Upload);
  await uploadNativeModel(new File(["model"], "model.gguf"), vi.fn(), new AbortController().signal);
  expect(setHeader).toHaveBeenCalledWith("X-Ryn-Local-Token", "launch-token");
});

it("desktop bootstrap covers direct fetch and never attaches the token to other origins", async () => {
  Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: { invoke: vi.fn(async (name: string) => name === "local_token" ? "launch-token" : 8791) } });
  const fetcher = vi.fn(async () => Response.json({}));
  vi.stubGlobal("fetch", fetcher);
  await initializeDesktopConnection();
  await fetch("http://localhost/api/local/drop/status", { headers: { "X-Other": "preserved" } });
  await fetch("https://external.example.test/api/local/status");
  await fetch("http://localhost/public");
  const calls = fetcher.mock.calls as unknown as [string, RequestInit?][];
  expect(new Headers(calls[0][1]?.headers).get("X-Ryn-Local-Token")).toBe("launch-token");
  expect(new Headers(calls[0][1]?.headers).get("X-Other")).toBe("preserved");
  expect(calls[1][1]).toBeUndefined();
  expect(calls[2][1]).toBeUndefined();
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
});

it("desktop SSE sends the launch header and parses named and multiline events", async () => {
  Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: { invoke: vi.fn(async () => "launch-token") } });
  let controller: ReadableStreamDefaultController<Uint8Array>;
  const fetcher = vi.fn(async () => new Response(new ReadableStream<Uint8Array>({ start(value) { controller = value; } })));
  vi.stubGlobal("fetch", fetcher);
  const source = localEventSource("http://localhost/api/local/messages/stream");
  const received = new Promise<MessageEvent>(resolve => source.addEventListener("delta", event => resolve(event as MessageEvent)));
  await vi.waitFor(() => expect(fetcher).toHaveBeenCalled());
  controller!.enqueue(new TextEncoder().encode("event: delta\r\nid: 4\r\ndata: first\r\ndata: sec"));
  controller!.enqueue(new TextEncoder().encode("ond\r\n\r\n"));
  const event = await received;
  expect(event.data).toBe("first\nsecond");
  expect(event.lastEventId).toBe("4");
  expect(new Headers((fetcher.mock.calls as unknown as [string, RequestInit][])[0][1].headers).get("X-Ryn-Local-Token")).toBe("launch-token");
  source.close(); controller!.close();
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
});
