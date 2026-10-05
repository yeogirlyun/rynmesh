export type DeviceNetwork = {
  id: string;
  label: string;
  scope: "local" | "remote" | "public" | "unknown";
  route: "local" | "lan" | "public" | "relay" | "unknown";
  address?: string;
};

// An advertised address is not evidence of a working route or LAN membership.
// In particular Peer.network identifies the overlay, not a physical network.
export function networkFromEndpoint(endpoint?: string): DeviceNetwork {
  const unknown: DeviceNetwork = {
    id: "unconfirmed", label: "Network unconfirmed", scope: "unknown", route: "unknown",
  };
  if (!endpoint) return unknown;
  try {
    const url = new URL(endpoint);
    if (!["http:", "https:"].includes(url.protocol)) return unknown;
    const address = url.hostname;
    const parts = address.split(".");
    if (parts.length !== 4 || parts.some(p => !/^\d+$/.test(p) || Number(p) > 255))
      return { ...unknown, address };
    const [a, b, c] = parts.map(Number);
    const reserved = a === 0 || a === 10 || a === 127 || a >= 224 ||
      (a === 100 && b >= 64 && b <= 127) || (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) || (a === 192 && (b === 168 || b === 0 || (b === 88 && c === 99))) ||
      (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100))) ||
      (a === 203 && b === 0 && c === 113);
    return reserved ? { ...unknown, address } : {
      id: "public", label: "Public endpoints", scope: "public", route: "unknown", address,
    };
  } catch {
    return unknown;
  }
}

export function networkFromPeer(endpoint?: string, lanEndpoints?: string[], lanReachable = false): DeviceNetwork {
  if (lanEndpoints?.length) {
    let address: string | undefined;
    try { address = new URL(lanEndpoints[0]).hostname; } catch { /* A malformed URL is not displayed. */ }
    return { id: "local", label: "Current network", scope: "local",
      route: lanReachable ? "lan" : "unknown", address };
  }
  return networkFromEndpoint(endpoint);
}

export const routeLabel = (route?: DeviceNetwork["route"]) => ({
  local: "This device", lan: "LAN direct", public: "Public direct", relay: "Relay", unknown: "Route unconfirmed",
}[route || "unknown"]);
