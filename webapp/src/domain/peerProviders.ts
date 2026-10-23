import { nodeControlUrl } from "./nodeUrl";

export interface PeerProviderStatus {
  plugin_id: string;
  status: "signed_out" | "pending" | "connected";
  bound: boolean;
  selected_service_ids: string[];
}
export interface PeerProviderModel { service_id: string; name: string; }
export interface PeerProviderMemberDevice { peer_id: string; name: string; }
export interface PeerProviderMemberEntry { id: string; name: string; devices: PeerProviderMemberDevice[]; }
export interface PeerProviderSharedModelEntry { service_id: string; name: string; peer_id: string; }
export interface PeerProviderDirectory {
  memberCount: number;
  deviceCount: number;
  modelNames: string[];
  members?: PeerProviderMemberEntry[];
  models?: PeerProviderSharedModelEntry[];
}

async function request(pluginId: string, path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<Record<string, unknown>> {
  const response = await fetch(nodeControlUrl(`/peer-providers/${encodeURIComponent(pluginId)}/${path}`), {
    method, credentials: "include", cache: "no-store", signal,
    headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  // Do not render provider error bodies: they can contain upstream secrets.
  if (!response.ok) throw new Error("peer_provider_request_failed");
  const value: unknown = await response.json();
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("peer_provider_invalid_response");
  return value as Record<string, unknown>;
}

function status(raw: Record<string, unknown>, pluginId: string): PeerProviderStatus {
  const state = raw.status;
  if (!["signed_out", "pending", "connected"].includes(String(state)) || raw.plugin_id !== pluginId) throw new Error("peer_provider_invalid_response");
  return { plugin_id: pluginId, status: state as PeerProviderStatus["status"], bound: raw.bound === true,
    selected_service_ids: Array.isArray(raw.selected_service_ids) ? raw.selected_service_ids.filter((x): x is string => typeof x === "string") : [] };
}

function directory(raw: Record<string, unknown>): PeerProviderDirectory {
  const members = Array.isArray(raw.members) ? raw.members : [];
  const models = Array.isArray(raw.models) ? raw.models : [];
  const parsedMembers: PeerProviderMemberEntry[] = members
    .filter((m): m is Record<string, unknown> => !!m && typeof m === "object")
    .map(m => ({
      id: String(m.id || ""),
      name: String(m.name || m.id || ""),
      devices: (Array.isArray(m.devices) ? m.devices : [])
        .filter((d): d is Record<string, unknown> => !!d && typeof d === "object" && typeof d.peer_id === "string")
        .map(d => ({ peer_id: String(d.peer_id), name: String(d.name || d.peer_id) })),
    }))
    .filter(m => Boolean(m.id));
  const parsedModels: PeerProviderSharedModelEntry[] = models
    .filter((row): row is Record<string, unknown> => !!row && typeof row === "object")
    .map(row => {
      const svc = (row.service && typeof row.service === "object" ? row.service : row) as Record<string, unknown>;
      const name = String(svc.model_alias || svc.name || svc.package_id || "");
      return {
        service_id: String(svc.service_id || svc.package_id || name),
        name,
        peer_id: String(row.peer_id || svc.peer_id || ""),
      };
    })
    .filter(m => Boolean(m.name));
  return {
    memberCount: members.length,
    deviceCount: members.reduce((total: number, member) => total + (Array.isArray(member?.devices) ? member.devices.length : 0), 0),
    modelNames: models.map(row => String(row?.service?.model_alias || row?.service?.name || row?.service?.package_id || "")).filter(Boolean),
    members: parsedMembers,
    models: parsedModels,
  };
}

export const peerProviders = {
  status: async (id: string, signal?: AbortSignal) => status(await request(id, "status", "GET", undefined, signal), id),
  login: async (id: string) => {
    const raw = await request(id, "login/start", "POST", {});
    const url = new URL(String(raw.browser_url || ""));
    if (url.protocol !== "https:" || url.username || url.password || url.hash) throw new Error("peer_provider_invalid_response");
    // Protocol state and PKCE are needed in this link; access credentials never are.
    if ([...url.searchParams.keys()].some(key => /^(code|access_token|refresh_token|client_secret|token|secret)$/i.test(key))) throw new Error("peer_provider_invalid_response");
    return { browserUrl: url.href };
  },
  logout: (id: string) => request(id, "logout", "POST", {}),
  bind: (id: string) => request(id, "bind", "POST", {}),
  catalog: async (id: string) => directory(await request(id, "catalog")),
  refresh: async (id: string) => directory(await request(id, "refresh", "POST", {})),
  models: async (id: string): Promise<PeerProviderModel[]> => {
    const raw = await request(id, "models");
    return (Array.isArray(raw.models) ? raw.models : []).filter(row => typeof row?.service_id === "string")
      .map(row => ({ service_id: row.service_id as string, name: typeof row.name === "string" ? row.name : row.service_id as string }));
  },
  publish: (id: string, serviceIds: string[]) => request(id, "publish", "POST", { service_ids: serviceIds }),
};
