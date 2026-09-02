export type EndpointAddressClass =
  | "private LAN"
  | "public IP literal"
  | "unresolved hostname"
  | "blocked local/link-local"
  | "invalid";

function classifyIpv4(host: string): EndpointAddressClass | null {
  if (!/^\d{1,3}(?:\.\d{1,3}){3}$/.test(host)) return null;
  const octets = host.split(".").map(Number);
  if (octets.some((value) => value > 255)) return "invalid";
  const [a, b] = octets;
  if (
    a === 0 || a === 127 || (a === 169 && b === 254) ||
    a >= 224
  ) return "blocked local/link-local";
  if (a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168)) {
    return "private LAN";
  }
  return "public IP literal";
}

export function endpointAddressClass(endpoint: string): EndpointAddressClass {
  try {
    const parsed = new URL(endpoint);
    if (!(["http:", "https:"] as string[]).includes(parsed.protocol)) return "invalid";
    if (parsed.username || parsed.password || parsed.hash || endpoint.length > 2048) return "invalid";
    const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, "");
    if (
      !host || host === "localhost" || host === "metadata" ||
      host === "metadata.google.internal" || host === "::" || host === "::1"
    ) {
      return "blocked local/link-local";
    }
    if (host.startsWith("fe80:")) return "blocked local/link-local";
    if (host.startsWith("fc") || host.startsWith("fd")) return "private LAN";
    if (host.includes(":")) return "public IP literal";
    return classifyIpv4(host) ?? "unresolved hostname";
  } catch {
    return "invalid";
  }
}

export function splitEndpoints(value: string): string[] {
  return [...new Set(value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean))];
}
