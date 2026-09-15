import { afterEach, expect, it, vi } from "vitest";
import { aiAccess } from "./aiAccess";

afterEach(() => vi.unstubAllGlobals());

it("sends the friend's peer id in a POST body and excludes it from the URL", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true,
    json: async () => ({ peer_id: "alice", relationship_id: "a".repeat(32), checked_at: 0, status: "authorized", services: [] }) });
  vi.stubGlobal("fetch", fetch);
  const peer = "alice";
  await aiAccess.refresh(peer);
  const [url, options] = fetch.mock.calls[0];
  expect(url).toMatch(/\/api\/local\/ai-access\/friend-services$/);
  expect(url).not.toContain("peer_id=");
  expect(url).not.toContain(peer);
  expect(options.method).toBe("POST");
  expect(JSON.parse(options.body)).toEqual({ peer_id: peer });
});
