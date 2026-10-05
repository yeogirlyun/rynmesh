import { beforeEach, expect, it, vi } from "vitest";
import { makeFixtureNodeClient } from "./fixtureNodeClient";
import { readChatServices, writeChatServices } from "./llmServiceCache";

beforeEach(() => { localStorage.clear(); });

it("isolates snapshots by node/mode and network and stores only public metadata", async () => {
  const services = await makeFixtureNodeClient().listLLMServices();
  const augmented = services.map(service => ({ ...service, api_key: "secret-token", messages: ["private body"] }));
  writeChatServices("live:node-a", "network-a", "network-a", augmented);
  const cached = readChatServices("live:node-a", "network-a");
  expect(cached?.services[0].peer_id).toBe(services[0].peer_id);
  expect(readChatServices("fixture:node-a", "network-a")).toBeNull();
  expect(readChatServices("live:node-b", "network-a")).toBeNull();
  expect(readChatServices("live:node-a", "network-b")).toBeNull();
  expect(JSON.stringify(localStorage)).not.toContain("secret-token");
  expect(JSON.stringify(localStorage)).not.toContain("private body");
});

it("ignores expired, corrupt, and structurally invalid snapshots", async () => {
  const services = await makeFixtureNodeClient().listLLMServices();
  writeChatServices("node", "", "main", services);
  const key = localStorage.key(0)!;
  const raw = localStorage.getItem(key)!;
  const snapshot = JSON.parse(raw);
  localStorage.setItem(key, JSON.stringify({ ...snapshot, savedAt: Date.now() - 8 * 86400000 }));
  expect(readChatServices("node", "")).toBeNull();
  localStorage.setItem(key, "{");
  expect(readChatServices("node", "")).toBeNull();
  localStorage.setItem(key, JSON.stringify({ ...snapshot, services: [{ peer_id: "other" }] }));
  expect(readChatServices("node", "")).toBeNull();
  localStorage.setItem(key, raw);
  expect(readChatServices("node", "")?.networkId).toBe("main");
});

it("tolerates unavailable or full local storage", () => {
  const read = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("unavailable"); });
  const write = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota"); });
  try {
    expect(readChatServices("node", "")).toBeNull();
    expect(() => writeChatServices("node", "", "main", [])).not.toThrow();
  } finally {
    read.mockRestore();
    write.mockRestore();
  }
});
