import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MessageBubble, type ChatRecord } from "./ChatParts";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__; });

it("desktop attachments load with a credential and render an owned blob URL", async () => {
  Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: { invoke: vi.fn(async () => "launch-token") } });
  const create = vi.fn(() => "blob:attachment");
  const revoke = vi.fn();
  vi.stubGlobal("URL", Object.assign(class extends URL {}, { createObjectURL: create, revokeObjectURL: revoke }));
  const fetcher = vi.fn(async () => new Response("bytes", { headers: { "Content-Type": "image/png" } }));
  vi.stubGlobal("fetch", fetcher);
  const record = { dir: "in", kind: "image", text: "", ts: "2026-11-17T00:00:00Z", attachment: { filename: "photo.png", mime: "image/png" } } as ChatRecord;
  const view = render(<MessageBubble record={record} url="http://localhost/api/local/messages/attachment/id?peer_id=peer" peerName="Peer" />);
  await waitFor(() => expect(screen.getByRole("img", { name: "photo.png" })).toHaveAttribute("src", "blob:attachment"));
  expect(new Headers((fetcher.mock.calls as unknown as [string, RequestInit][])[0][1].headers).get("X-Ryn-Local-Token")).toBe("launch-token");
  view.unmount();
  expect(revoke).toHaveBeenCalledWith("blob:attachment");
});
