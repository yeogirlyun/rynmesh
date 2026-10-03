/** Product metadata only. Advertised prices and provider identity come from the node. */
export interface ServiceDescriptor {
  id: string;
  capability: string;
  operation: string;
  pricing: { unit: "tokens" | "job" | "session"; label: string };
  region?: { code: string; label: string };
  discoveryIntervalMs: number;
  orderIntervalMs: number;
}

export const serviceDescriptors = {
  privateAI: { id: "private-ai", capability: "text-generation", operation: "infer",
    pricing: { unit: "tokens", label: "Tokens" }, discoveryIntervalMs: 15_000, orderIntervalMs: 1500 },
  video: { id: "video-rendering", capability: "signal50.veo_motion.v1",
    operation: "signal50.remote_action.complete_flow_video_veo_motion_clips",
    pricing: { unit: "job", label: "Maximum cost" }, discoveryIntervalMs: 15_000, orderIntervalMs: 1500 },
  secureWeb: { id: "secure-web-access", capability: "egress", operation: "connect",
    region: { code: "CN", label: "Mainland China" }, pricing: { unit: "session", label: "Price" },
    discoveryIntervalMs: 15_000, orderIntervalMs: 5000 },
} satisfies Record<string, ServiceDescriptor>;

export function providerIdentity(network: string, peer: string, service: string) {
  return JSON.stringify([network, peer, service]);
}
