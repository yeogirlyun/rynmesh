import { describe, expect, it } from "vitest";
import {
  createLatestRequestGate,
  llmProviderLabel,
  llmServiceAvailability,
  llmServicePricingLabel,
  llmServiceRecordKey,
} from "./llmOrders";
import type { LLMServiceRecord } from "./nodeClient";

const record: LLMServiceRecord = {
  peer_id: "peer:provider",
  node_name: "Provider",
  online: true,
  capacity: { available: 1 },
  service: {
    package_id: "package",
    model_alias: "alias",
    capabilities: ["text-generation"],
    context_window: 4096,
    max_output_tokens: 256,
    pricing: {
      currency: "CREDITS", input_per_1k: 1, output_per_1k: 2,
      minimum: 0.5, maximum_per_task: 10,
    },
    privacy: {},
  },
};

describe("LLM service selection helpers", () => {
  it("keeps identity compound and exposes comparison labels", () => {
    expect(llmServiceRecordKey(record)).toBe("peer:provider::package");
    expect(llmProviderLabel(record)).toBe("Provider");
    expect(llmServiceAvailability(record)).toBe("ready");
    expect(llmServicePricingLabel(record)).toBe("1 in / 2 out CREDITS per 1k");
    expect(llmServiceAvailability({ ...record, capacity: { available: 0 } })).toBe("busy");
    expect(llmServiceAvailability({ ...record, online: false })).toBe("offline");
  });

  it("rejects stale asynchronous bucket generations", () => {
    const gate = createLatestRequestGate();
    const slowFirstLoad = gate.begin();
    const newerLoad = gate.begin();
    expect(gate.isCurrent(slowFirstLoad)).toBe(false);
    expect(gate.isCurrent(newerLoad)).toBe(true);
  });
});
