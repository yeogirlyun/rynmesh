import type { LLMServiceRecord } from "./nodeClient";

// Terminal task states, mirroring rynmesh/llm_package/task_protocol.py
// TERMINAL_STATES. One definition — the screens used to each carry their own.
export const LLM_TERMINAL_STATES = new Set([
  "succeeded",
  "failed",
  "timed_out",
  "cancelled",
  "rejected",
]);

// Canonical identity for a discovered LLM service. Model aliases are not
// unique, so identity is (provider peer, package). This format is also the
// Private AI conversation-store key, so it must stay stable.
export function llmServiceKey(peerId: string, packageId: string): string {
  return `${peerId}::${packageId}`;
}

export function llmServiceRecordKey(service: LLMServiceRecord): string {
  return llmServiceKey(service.peer_id, service.service.package_id);
}

export type LLMServiceAvailability = "ready" | "busy" | "offline";

export function llmServiceAvailability(service: LLMServiceRecord): LLMServiceAvailability {
  if (!service.online) return "offline";
  if (service.capacity?.available !== undefined && service.capacity.available <= 0) return "busy";
  return "ready";
}

export function llmProviderLabel(service: LLMServiceRecord): string {
  return service.node_name?.trim() || service.peer_id;
}

export function shortPeerId(peerId: string): string {
  if (peerId.length <= 22) return peerId;
  return `${peerId.slice(0, 12)}…${peerId.slice(-7)}`;
}

export function llmServicePricingLabel(service: LLMServiceRecord): string {
  const pricing = service.service.pricing;
  return `${pricing.input_per_1k} in / ${pricing.output_per_1k} out ${pricing.currency} per 1k`;
}

/** A tiny generation gate for async UI loads where only the newest may commit. */
export function createLatestRequestGate() {
  let generation = 0;
  return {
    begin: () => { generation += 1; return generation; },
    isCurrent: (candidate: number) => candidate === generation,
  };
}
