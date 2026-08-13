import { useEffect, useState } from "react";
import type { NodeClient } from "../../domain/nodeClient";
import type { JobCapacity } from "../../domain/types";
import VpnServiceCard from "./VpnServiceCard";

const VPN_CAPABILITY = "net.egress";

interface CapGroup {
  capability: string;
  providerCount: number;
}

export default function RecommendedServices({ client }: { client: NodeClient }) {
  const [groups, setGroups] = useState<CapGroup[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const caps: JobCapacity[] = await client.listJobCapacities();
        const byCap = new Map<string, number>();
        for (const c of caps) {
          for (const cap of c.capabilities ?? []) {
            byCap.set(cap, (byCap.get(cap) ?? 0) + 1);
          }
        }
        if (alive) {
          setGroups([...byCap.entries()].map(([capability, providerCount]) => ({ capability, providerCount })));
        }
      } catch {
        if (alive) setGroups([]);
      } finally {
        if (alive) setLoaded(true);
      }
    })();
    return () => { alive = false; };
  }, [client]);

  const hasVpn = groups.some((g) => g.capability === VPN_CAPABILITY);
  const others = groups.filter((g) => g.capability !== VPN_CAPABILITY);

  return (
    <section className="recommended-services">
      <h2 className="section-title">Recommended Services</h2>
      {!loaded && <p className="service-empty">Loading services…</p>}
      {hasVpn && <VpnServiceCard client={client} />}
      {others.length > 0 && (
        <div className="service-chips">
          {others.map((g) => (
            <span key={g.capability} className="service-chip" title={`${g.providerCount} provider(s)`}>
              {g.capability} · available
            </span>
          ))}
        </div>
      )}
      {loaded && !hasVpn && others.length === 0 && (
        <p className="service-empty">No services discovered yet — connect to a peer to see recommendations.</p>
      )}
    </section>
  );
}
