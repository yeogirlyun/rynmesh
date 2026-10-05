import type { LLMServiceRecord } from "./nodeClient";

const PREFIX = "ryn-ai-services-v1:";
const MAX_AGE = 7 * 24 * 60 * 60 * 1000;

interface ServiceSnapshot {
  networkId: string;
  services: LLMServiceRecord[];
  savedAt: number;
}

// Cache only public discovery metadata. Messages stay in the encrypted
// conversation store; credentials and provider connection URLs never go here.
function publicService(value: LLMServiceRecord): LLMServiceRecord {
  const service = value.service;
  if (typeof value.peer_id !== "string" || !value.peer_id ||
      typeof value.online !== "boolean" || typeof service?.package_id !== "string" ||
      !service.package_id || typeof service.model_alias !== "string" ||
      !Array.isArray(service.capabilities) || !service.capabilities.every(item => typeof item === "string") ||
      !Number.isFinite(service.context_window) || !Number.isFinite(service.max_output_tokens) ||
      typeof service.pricing?.currency !== "string" ||
      ![service.pricing.input_per_1k, service.pricing.output_per_1k,
        service.pricing.minimum, service.pricing.maximum_per_task].every(Number.isFinite)) {
    throw new Error("Invalid cached AI service");
  }
  return {
    peer_id: value.peer_id,
    node_name: typeof value.node_name === "string" ? value.node_name : undefined,
    local_only: value.local_only === true,
    online: value.online,
    service: {
      package_id: service.package_id,
      model_alias: service.model_alias,
      adapter: typeof service.adapter === "string" ? service.adapter : undefined,
      capabilities: service.capabilities,
      context_window: service.context_window,
      max_output_tokens: service.max_output_tokens,
      pricing: {
        currency: service.pricing.currency,
        input_per_1k: service.pricing.input_per_1k,
        output_per_1k: service.pricing.output_per_1k,
        minimum: service.pricing.minimum,
        maximum_per_task: service.pricing.maximum_per_task,
      },
      privacy: {
        policy_text: typeof service.privacy?.policy_text === "string" ? service.privacy.policy_text : undefined,
        compute_node_sees_plaintext: service.privacy?.compute_node_sees_plaintext === true,
      },
    },
  };
}

// Origin storage plus mode, current node identity and requested network keep
// fixture data and different nodes/networks from sharing discovery snapshots.
function cacheKey(scope: string, requestedNetwork: string) {
  return PREFIX + JSON.stringify([scope, requestedNetwork]);
}

export function readChatServices(scope: string, requestedNetwork: string): ServiceSnapshot | null {
  try {
    const raw = localStorage.getItem(cacheKey(scope, requestedNetwork));
    if (!raw) return null;
    const snapshot = JSON.parse(raw) as ServiceSnapshot;
    if (!Number.isFinite(snapshot.savedAt) || snapshot.savedAt > Date.now() ||
        Date.now() - snapshot.savedAt > MAX_AGE ||
        typeof snapshot.networkId !== "string" || !snapshot.networkId ||
        (requestedNetwork && snapshot.networkId !== requestedNetwork) ||
        !Array.isArray(snapshot.services) || snapshot.services.length > 100) return null;
    return { ...snapshot, services: snapshot.services.map(publicService) };
  } catch {
    // Disabled storage or an old/corrupt snapshot must not prevent discovery.
    return null;
  }
}

export function writeChatServices(scope: string, requestedNetwork: string, networkId: string, services: LLMServiceRecord[]) {
  try {
    localStorage.setItem(cacheKey(scope, requestedNetwork), JSON.stringify({
      networkId, services: services.slice(0, 100).map(publicService), savedAt: Date.now(),
    } satisfies ServiceSnapshot));
  } catch {
    // Quota/private-browsing failures only disable this startup optimization.
  }
}
