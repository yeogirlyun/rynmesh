import { nodeControlUrl } from "./nodeUrl";

/** A web page another device opened to this one (served from that device). */
export interface RemoteSite { site_id: string; title: string; entry: string }
export interface RemoteSiteDevice {
  provider_resource: string;
  peer_id: string;
  member_name: string;
  device_name: string;
  online: boolean;
  sites: RemoteSite[];
  error?: string;
}
export interface Audience { mode: "all" | "members"; members: string[] }
/** A page this device hosts. The backend token is never returned after registration. */
export interface HostedSite {
  owner_path?: string | null;
  available?: boolean;
  site_id: string;
  title: string;
  port: number;
  entry: string;
  enabled: boolean;
  audience: Audience;
  updated_at: string;
}
export interface SiteMember { provider_resource: string; member_id: string; member_name: string }
export interface OpenedSite {
  key: string;
  url: string;
  entry: string;
  title: string;
  peer_id: string;
  provider_resource: string;
  member_name: string;
  device_name: string;
}
export interface OpenedSiteStatus {
  key: string;
  site_id: string;
  peer_id: string;
  online: boolean;
  route: string | null;
  member_name: string;
  device_name: string;
  last_seen: string | null;
  origin: string;
}

export class NodeSiteError extends Error {
  constructor(readonly code: string, readonly status: number) { super(code); }
}

// Bound the entire browser request (including its body), not just the peer
// probe on the node. A stalled request must not hold the polling lock forever.
async function statusRequest(key: string, fresh: boolean): Promise<OpenedSiteStatus> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout>;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new NodeSiteError("site_unavailable", 0));
    }, 6_000);
  });
  try {
    return await Promise.race([
      call<OpenedSiteStatus>(`/opened/${encodeURIComponent(key)}${fresh ? "?fresh=true" : ""}`,
        { signal: controller.signal }),
      deadline,
    ]);
  } finally {
    clearTimeout(timer!);
  }
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(nodeControlUrl(`/node-sites${path}`), {
    credentials: "include",
    cache: "no-store",
    ...init,
    headers: init.body ? { "Content-Type": "application/json", ...(init.headers || {}) } : init.headers,
  });
  let body: unknown = null;
  try { body = await response.json(); } catch { body = null; }
  if (!response.ok) {
    const code = body && typeof body === "object" && typeof (body as { code?: unknown }).code === "string"
      ? (body as { code: string }).code : "site_unavailable";
    throw new NodeSiteError(code, response.status);
  }
  return body as T;
}

export const nodeSites = {
  remote: (refresh = false) =>
    call<{ devices: RemoteSiteDevice[] }>(`/remote${refresh ? "?refresh=true" : ""}`).then(value => value.devices),
  open: (device: Pick<RemoteSiteDevice, "provider_resource" | "peer_id">, siteId: string) =>
    call<OpenedSite>("/open", { method: "POST", body: JSON.stringify({
      provider_resource: device.provider_resource, peer_id: device.peer_id, site_id: siteId }) }),
  status: (key: string, fresh = false) =>
    statusRequest(key, fresh),
  mine: () => call<{ sites: HostedSite[]; members: SiteMember[] }>("/mine"),
  update: (siteId: string, changes: Partial<Pick<HostedSite, "enabled" | "audience" | "title">>) =>
    call<{ site: HostedSite }>(`/mine/${encodeURIComponent(siteId)}`, { method: "PATCH", body: JSON.stringify(changes) })
      .then(value => value.site),
};
