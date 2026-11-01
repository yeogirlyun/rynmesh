import { describe, expect, it } from "vitest";
import type { LLMServiceRecord } from "./nodeClient";
import { isServiceUsable, llmServiceAvailability } from "./llmOrders";

const record = (more: Partial<LLMServiceRecord> = {}): LLMServiceRecord => ({
  peer_id: "remote", online: true,
  service: {
    package_id: "codex-cli", model_alias: "Codex", capabilities: [], context_window: 1, max_output_tokens: 1,
    pricing: { currency: "credit", input_per_1k: 0, output_per_1k: 0, minimum: 0, maximum_per_task: 0 },
    privacy: {},
  },
  ...more,
});

describe("isServiceUsable", () => {
  it("trusts the node's availability over the provider's online claim", () => {
    const advertised = record({ online: true, availability: { state: "needs_connection", reason: "advertisement_only" } });
    expect(isServiceUsable(advertised)).toBe(false);
    expect(isServiceUsable(record({ online: false, availability: { state: "ready", reason: "space_member" } }))).toBe(true);
    expect(isServiceUsable(record({ availability: { state: "offline", reason: "peer_unreachable" } }))).toBe(false);
  });
  it("falls back to the claim for records without a view model (cached snapshots, older nodes)", () => {
    expect(isServiceUsable(record({ online: true }))).toBe(true);
    expect(isServiceUsable(record({ online: false }))).toBe(false);
  });
});

describe("llmServiceAvailability", () => {
  it("never describes an unconnected device's advertisement as available", () => {
    const text = llmServiceAvailability(record({ availability: { state: "needs_connection", reason: "advertisement_only" } }));
    expect(text).toMatch(/Not connected/);
    expect(text).not.toMatch(/Available in discovery/);
  });
  it("explains when Personal Space AI access is the missing piece", () => {
    expect(llmServiceAvailability(record({ availability: { state: "needs_connection", reason: "space_ai_access_off" } })))
      .toMatch(/AI access is off/);
  });
  it("keeps the existing wording for records without a view model", () => {
    expect(llmServiceAvailability(record())).toBe("Available in discovery");
    expect(llmServiceAvailability(record({ online: false }))).toBe("Availability unknown — refresh services");
  });
});
