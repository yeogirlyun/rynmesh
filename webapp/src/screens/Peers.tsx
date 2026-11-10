import { tr, useUILanguage, uiLocale } from "../uiI18n";
import { Copy, Radar, Search, ShieldAlert, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { useAppContext } from "../appContext";
import { LoadingPanel } from "../components/ui";
import type { IdentityTier, JobCapacity, Peer, PeerHealth } from "../domain/types";
import { joinPeerMeta, type PeerMeta } from "../domain/peerMeta";
import { DeviceTile, deviceHue, type Tone } from "../personal/drop/FamilyParts";
import "./peers-refresh.css";

const tierValues: Array<IdentityTier | "all"> = ["all", "unverified", "attested", "staked", "proven"];
const tierTone: Record<IdentityTier, Tone> = { proven: "ok", staked: "self", attested: "sky", unverified: "off" };
const shortPeerId = (id: string) => (id.length > 16 ? `${id.slice(0, 9)}…${id.slice(-5)}` : id);
const isAnonymous = (peer: Peer) => !peer.name || peer.name === peer.id || /^peer:/i.test(peer.name);
const statusOf = (meta: PeerMeta | undefined): { tone: Tone; label: string; dot: "on" | "off" | "unknown" } =>
  meta?.online === true ? { tone: "ok", label: tr("online"), dot: "on" } : meta?.online === false ? { tone: "off", label: tr("offline"), dot: "off" } : { tone: "warn", label: tr("Status unknown"), dot: "unknown" };
const trustOf = (peer: Peer): { tone: Tone; label: string } =>
  peer.quarantined ? { tone: "bad", label: tr("quarantined") } : peer.trustedRoot ? { tone: "self", label: tr("trusted root") } : { tone: "off", label: tr("local default") };

function PeerTile({ peer, meta, size }: { peer: Peer; meta: PeerMeta | undefined; size: "sm" | "xl" }) {
  const online = meta?.online;
  return <DeviceTile kind={isAnonymous(peer) ? "mesh" : "desktop"} hue={isAnonymous(peer) ? "slate" : deviceHue(peer.id, { online })} size={size === "xl" ? "xl" : "sm"} off={online === false} />;
}

export default function Peers() {
  useUILanguage();
  const { client, confirm, notify } = useAppContext();
  const [peers, setPeers] = useState<Peer[]>([]);
  const [capacities, setCapacities] = useState<JobCapacity[]>([]);
  const [health, setHealth] = useState<PeerHealth[]>([]);
  const [query, setQuery] = useState("");
  const [tier, setTier] = useState<IdentityTier | "all">("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [copied, setCopied] = useState(false);

  const meta: Map<string, PeerMeta> = joinPeerMeta(peers.map((p) => p.id), health, capacities);

  const refresh = async () => {
    setLoading(true);
    const [result, caps] = await Promise.all([
      client.listPeers({ tier, search: query }),
      client.listJobCapacities(),
    ]);
    setPeers(result);
    setCapacities(caps);
    setLoading(false);
  };

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, tier]);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const h = await client.peersHealth();
        if (alive) setHealth(h);
      } catch {
        /* keep last-known health; never block the list */
      }
    };
    void tick();
    const id = window.setInterval(tick, 10000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [client]);

  if (loading) return <div className="rf rd-peers"><LoadingPanel /></div>;

  const needle = query.trim().toLowerCase();
  const shown = needle ? peers.filter((peer) => [peer.name, peer.id, peer.endpoint].some((value) => value?.toLowerCase().includes(needle))) : peers;
  const selected = shown.find((peer) => peer.id === selectedId) ?? shown[0] ?? null;
  const selectedMeta = selected ? meta.get(selected.id) : undefined;
  const online = peers.filter((peer) => meta.get(peer.id)?.online === true).length;
  const roots = peers.filter((peer) => peer.trustedRoot && !peer.quarantined).length;
  const quarantined = peers.filter((peer) => peer.quarantined).length;
  const num = (value: number) => value.toLocaleString(uiLocale());
  const pct = (peer: Peer) => `${Math.max(0, Math.min(100, Math.round(peer.weight * 100)))}%`;

  return (
    <div className="rf rd-peers">
      <header className="rd-head">
        <div className="rd-head-text">
          <h1>{tr("Connection details")}</h1>
          <p>{tr("Inspect device identities, connection status, and trust.")}</p>
        </div>
        <button type="button" className="rf-btn" onClick={async () => {
          await client.discoverPeers();
          notify("ok", tr("Peer discovery requested through local node"));
        }}><Radar size={15} strokeWidth={1.6} aria-hidden="true" />{tr("Discover")}</button>
      </header>

      <section className="rd-sum" aria-label={tr("Peers")}>
        <div><span>{tr("Peers")}</span><b>{num(peers.length)}</b></div>
        <div><span><i className="rf-t-ok" aria-hidden="true" />{tr("Online")}</span><b>{num(online)}</b></div>
        <div><span><i className="rf-t-self" aria-hidden="true" /><em className="rd-cap">{tr("trusted root")}</em></span><b>{num(roots)}</b></div>
        <div><span><i className="rf-t-bad" aria-hidden="true" /><em className="rd-cap">{tr("quarantined")}</em></span><b>{num(quarantined)}</b></div>
      </section>

      <div className="rd-filter">
        <div className="rf-seg" role="group" aria-label={tr("Tier")}>
          {tierValues.map((candidate) => (
            <button key={candidate} type="button" aria-pressed={tier === candidate} onClick={() => setTier(candidate)}>
              {tr(candidate)}{tier === candidate ? <em>{peers.length}</em> : null}
            </button>
          ))}
        </div>
        <label className="rd-search">
          <Search size={15} strokeWidth={1.6} aria-hidden="true" />
          <input value={query} aria-label={tr("Search")} placeholder={tr("Search name, ID or address")} onChange={(event) => setQuery(event.target.value)} onBlur={() => void refresh()} />
        </label>
      </div>

      <div className="rd-cols">
        <div className="rf-card rd-list">
          <div className="rd-lh" aria-hidden="true"><span className="c-peer">{tr("Peer")}</span><span className="c-tier">{tr("Tier")}</span><span className="c-weight">{tr("Weight")}</span><span className="c-seen">{tr("Last seen")}</span><span className="c-trust">{tr("Trust status")}</span></div>
          {shown.length === 0 ? <p className="rd-none">{tr("No peers to show.")}</p> : shown.map((peer) => {
            const m = meta.get(peer.id);
            const st = statusOf(m);
            const tr2 = trustOf(peer);
            return (
              <button key={peer.id} type="button" className={`rd-row rf-h-${isAnonymous(peer) ? "slate" : deviceHue(peer.id, { online: m?.online })}`} data-on={selected?.id === peer.id ? "true" : "false"} data-off={m?.online === false ? "true" : "false"}
                aria-pressed={selected?.id === peer.id} onClick={() => setSelectedId(peer.id)}>
                <span className="c-peer"><PeerTile peer={peer} meta={m} size="sm" />
                  <span className="rd-who">
                    <span className="rd-name"><i className={`rd-dot ${st.dot}`} role="img" aria-label={st.label} /><b className="rf-ell">{peer.name}</b></span>
                    <small className="rf-mono rf-ell">{peer.endpoint}</small>
                    {(m?.capabilities.length ?? 0) > 0 ? <span className="rd-caps">{m!.capabilities.map((cap) => <span key={cap} className="rf-tag">{cap}</span>)}</span> : null}
                  </span>
                </span>
                <span className="c-tier"><span className={`rf-tag rf-tag-tone rf-t-${tierTone[peer.tier]}`}>{tr(peer.tier.charAt(0).toUpperCase() + peer.tier.slice(1))}</span></span>
                <span className="c-weight"><span className="rd-w"><span className="rf-prog" role="img" aria-label={`${tr("Weight")} ${peer.weight.toFixed(2)}`}><i style={{ width: pct(peer) }} /></span><b>{peer.weight.toFixed(2)}</b></span>
                  <small>{tr("{{n}} credits", { n: num(peer.credits) })}</small></span>
                <span className="c-seen"><span>{peer.lastSeen}</span><small>{tr("Served/Fetched")} {num(peer.served)} / {num(peer.fetched)}</small></span>
                <span className="c-trust"><span className={`rf-pill rf-pill-sm rf-t-${tr2.tone}`}><i aria-hidden="true" /><span className="rd-cap">{tr2.label}</span></span></span>
              </button>
            );
          })}
          <p className="rf-note rd-list-note"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0zM12 16v-4M12 8h.01" /></svg>{tr("Served / fetched counts come from this device's local records.")}</p>
        </div>

        {selected ? (
          <aside className={`rf-card rd-detail rf-h-${isAnonymous(selected) ? "slate" : deviceHue(selected.id, { online: selectedMeta?.online })}`} aria-label={selected.name}>
            <div className="rd-detail-head">
              <PeerTile peer={selected} meta={selectedMeta} size="xl" />
              <div className="rd-detail-id">
                <h2 id="peer-detail-title">{selected.name}</h2>
                <div className="rd-detail-pills">
                  <span className={`rf-pill rf-t-${statusOf(selectedMeta).tone}`}><i aria-hidden="true" /><span className="rd-cap">{statusOf(selectedMeta).label}</span></span>
                  <span className={`rf-pill rf-pill-line rf-t-${trustOf(selected).tone}`}><span className="rd-cap">{trustOf(selected).label}</span></span>
                </div>
              </div>
            </div>
            <div className="rd-detail-body">
              <dl>
                <div className="rf-dl-row"><dt>{tr("Peer ID")}</dt><dd className="rd-idline"><code className="rf-code-chip" title={selected.id}>{shortPeerId(selected.id)}</code>
                  <button type="button" className="rf-ib rf-ib-sm" aria-label={tr("Copy {{v0}}", { v0: selected.id })} onClick={async () => {
                    try { await navigator.clipboard.writeText(selected.id); setCopied(true); window.setTimeout(() => setCopied(false), 1500); } catch { /* clipboard may be blocked */ }
                  }}><Copy size={13} strokeWidth={1.8} aria-hidden="true" /></button>{copied ? <small role="status">{tr("Copied")}</small> : null}</dd></div>
                <div className="rf-dl-row"><dt>{tr("Endpoint")}</dt><dd className="rf-mono rd-endpoint">{selected.endpoint}</dd></div>
                <div className="rf-dl-row"><dt>{tr("Services")}</dt><dd className="rd-svc">{(selectedMeta?.capabilities.length ?? 0) > 0
                  ? selectedMeta!.capabilities.map((cap) => <span key={cap} className="rf-tag">{cap}</span>) : <span className="rd-muted">{tr("none")}</span>}</dd></div>
                <div className="rf-dl-row"><dt>{tr("Tier")}</dt><dd><span className={`rf-tag rf-tag-tone rf-t-${tierTone[selected.tier]}`}>{tr(selected.tier.charAt(0).toUpperCase() + selected.tier.slice(1))}</span></dd></div>
                <div className="rf-dl-row"><dt>{tr("Credits")}</dt><dd>{num(selected.credits)}</dd></div>
                <div className="rf-dl-row"><dt>{tr("Weight")}</dt><dd className="rd-weight"><span className="rf-prog" aria-hidden="true"><i style={{ width: pct(selected) }} /></span><b>{selected.weight.toFixed(2)}</b></dd></div>
                <div className="rf-dl-row"><dt>{tr("Last seen")}</dt><dd>{selected.lastSeen}</dd></div>
                <div className="rf-dl-row"><dt>{tr("Served/Fetched")}</dt><dd>{num(selected.served)} / {num(selected.fetched)}</dd></div>
              </dl>
              <section aria-labelledby="trust-title" className="rd-trust">
                <h3 id="trust-title">{tr("Trust on this device")}</h3>
                <div className="rd-trust-list">
                  <div><div><b>{tr("Trust as root")}</b><small>{tr("Treat signed evidence from this peer as trusted root evidence.")}</small></div>
                    <button type="button" className="rf-btn rf-btn-p rf-btn-sm" disabled={Boolean(selected.trustedRoot)}
                      onClick={() => confirm({
                        title: tr("Trust this peer as a root?"),
                        body: tr("This changes local identity policy. The node will treat signed evidence from this peer as trusted root evidence."),
                        risk: "high",
                        confirmLabel: tr("Trust root"),
                        onConfirm: () => notify("ok", tr("Trust root change sent to local node")),
                      })}><ShieldCheck size={14} strokeWidth={1.8} aria-hidden="true" />{tr("Trust root")}</button></div>
                  <div><div><b>{tr("Downrank locally")}</b><small>{tr("Lower this peer's weight on this device only.")}</small></div>
                    <button type="button" className="rf-btn rf-btn-sm">{tr("Downrank")}</button></div>
                  <div className="rd-trust-bad"><div><b>{tr("Quarantine")}</b><small>{tr("The local node will downrank this peer and prepare a signed report if configured.")}</small></div>
                    <button type="button" className="rf-btn rf-btn-sm rf-btn-d" disabled={Boolean(selected.quarantined)}
                      onClick={() => confirm({
                        title: tr("Quarantine this peer?"),
                        body: tr("The local node will downrank this peer and prepare a signed report if configured."),
                        risk: "high",
                        confirmLabel: tr("Quarantine"),
                        onConfirm: () => notify("warn", tr("Quarantine request sent to local node")),
                      })}><ShieldAlert size={14} strokeWidth={1.8} aria-hidden="true" />{tr("Quarantine")}</button></div>
                </div>
                <p className="rf-hint">{tr("These change local identity policy and ask for confirmation first.")}</p>
              </section>
            </div>
          </aside>
        ) : null}
      </div>
    </div>
  );
}
