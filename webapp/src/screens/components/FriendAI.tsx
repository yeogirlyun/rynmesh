import { ArrowRight, RefreshCw, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useAppContext } from "../../appContext";
import { aiAccess, type AIGrant, type FriendAISnapshot } from "../../domain/aiAccess";
import type { FriendRecord } from "../../domain/friendTypes";
import type { LLMProviderStatus } from "../../domain/nodeClient";
import AIInvitations from "./AIInvitations";
import { relativeAgo } from "../../personal/drop/FamilyParts";
import { tr, uiLocale, useUILanguage } from "../../uiI18n";
import { errorText } from "./friendLinks";
import { Tinted, formatWhen } from "./FriendsParts";
import "../friends-refresh.css";

/**
 * AI sharing with friends. Without `friend` every active friend gets a section; with `friend` only that one does
 * (`null` shows none). `onAllowed` reports which relationships may use the current local service, for the page summary.
 */
export default function FriendAI({ friends, friend: only, onAllowed }: { friends: FriendRecord[]; friend?: FriendRecord | null; onAllowed?: (relationshipIds: string[]) => void }) {
  useUILanguage();
  const [params] = useSearchParams();
  const heading = useRef<HTMLHeadingElement>(null);
  const { client, confirm } = useAppContext();
  const [grants, setGrants] = useState<AIGrant[]>([]);
  const [remote, setRemote] = useState<FriendAISnapshot[]>([]);
  const [provider, setProvider] = useState<LLMProviderStatus | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const epoch = useRef(0);
  const working = useRef(false);
  const active = friends.filter((friend) => friend.status === "active");
  const shown = only === undefined ? active : only ? active.filter((friend) => friend.relationship_id === only.relationship_id) : [];
  const report = useRef(onAllowed);
  report.current = onAllowed;
  const allowedKey = loaded ? grants.filter((row) => row.effective && row.service_id === provider?.service?.package_id).map((row) => row.relationship_id).sort().join(",") : null;
  useEffect(() => { if (allowedKey !== null) report.current?.(allowedKey ? allowedKey.split(",") : []); }, [allowedKey]);
  const serviceChanged = Boolean(params.get("share") && provider?.service?.package_id !== params.get("share"));
  const pricing = provider?.service?.pricing;
  const price = pricing ? `Development Task Balance: ${pricing.input_per_1k} input / ${pricing.output_per_1k} output per 1k tokens; minimum ${pricing.minimum}, task maximum ${pricing.maximum_per_task}. This is not RMB billing.` : "Price unknown; check the service configuration before sharing.";
  useEffect(() => { if (loaded && params.has("share")) heading.current?.focus(); }, [loaded, params]);
  const load = async () => {
    const generation = ++epoch.current;
    try {
      const [permissions, snapshots, status] = await Promise.all([aiAccess.list(), aiAccess.friends(), client.getLLMServiceStatus()]);
      if (generation !== epoch.current) return;
      setGrants(permissions.grants); setRemote(snapshots.friends); setProvider(status); setLoaded(true); setError("");
    } catch (reason) { if (generation === epoch.current) setError(errorText(reason, tr("AI settings could not be loaded."))); }
  };
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (!working.current) void load(); }, 5000);
    return () => { window.clearInterval(timer); epoch.current += 1; };
  }, [client]);
  const change = async (friend: FriendRecord, service: string, allowed: boolean, previous?: AIGrant) => {
    epoch.current += 1; working.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const result = await aiAccess.set(service, friend.relationship_id, allowed, previous?.revision ?? 0);
      epoch.current += 1;
      setGrants((rows) => [...rows.filter((row) => row.service_id !== service || row.relationship_id !== friend.relationship_id), result.grant]);
      setNotice(allowed ? tr("Permission saved for {{name}}. They can refresh their AI services.", { name: friend.node_name })
        : tr("Access revoked for {{name}}. New requests are blocked. Running computation may continue while cancellation is checked{{retry}}.", { name: friend.node_name, retry: result.cancellation === "pending" ? tr("; the node will retry the cancellation check") : "" }));
    } catch (reason) { setError(errorText(reason, tr("The permission change could not be confirmed."))); }
    finally { working.current = false; setBusy(false); }
  };
  const refresh = async (friend: FriendRecord) => {
    epoch.current += 1; working.current = true; setBusy(true); setError("");
    try {
      const snapshot = await aiAccess.refresh(friend.peer_id);
      epoch.current += 1;
      setRemote((rows) => [...rows.filter((row) => row.peer_id !== friend.peer_id), snapshot]);
    } catch (reason) {
      // Keep the friend's last confirmed snapshot and timestamp; only the error is new.
      setError(errorText(reason, tr("This friend could not be reached.")));
    } finally { working.current = false; setBusy(false); }
  };
  const service = provider?.service?.package_id;
  return <div className="rq-ai">
    {provider?.service && <AIInvitations serviceId={provider.service.package_id} disabled={busy || serviceChanged || !pricing || !provider.publication_enabled} />}
    <div className="rq-cards-head">
      <p className="rq-help">{tr("Being friends does not grant AI access. Choose who can use each service; your friend's device sees the questions sent to their model.")}</p>
      <button type="button" className="rf-ib rf-ib-sm" aria-label={tr("Refresh AI permissions")} title={tr("Refresh AI permissions")} disabled={busy} onClick={() => void load()}><RefreshCw size={14} strokeWidth={1.7} aria-hidden="true" /></button>
    </div>
    {loaded && serviceChanged ? <p role="alert">The selected service changed. Open the current model before sharing.</p> : null}{error ? <p className="rq-banner rq-banner-bad" role="alert">{error}</p> : null}{notice ? <p className="rq-banner rq-banner-ok" role="status">{notice}</p> : null}
    {!loaded ? <p className="rq-loading">{tr("Loading AI permissions…")}</p> : <>
      <section className="rq-ai-sec" aria-labelledby="rq-ai-mine">
        <h3 ref={heading} tabIndex={-1} id="rq-ai-mine">{tr("Your AI")}</h3>
        <div className="rq-box">
          {provider?.service ? <div className="rq-ai-row">
            <span className="rq-ai-tile rf-h-indigo" aria-hidden="true"><Sparkles size={18} strokeWidth={1.8} /></span>
            <div className="rq-ai-main"><b>{provider.service.model_alias}</b>
              <small>{price}</small><small>{provider.ready ? tr("Model ready") : tr("Model not ready")} · {provider.publication_enabled ? tr("Friend sharing enabled") : tr("Friend sharing paused")}</small></div>
            <Link className="rf-lnk rf-lnk-sm" to="/services/manage">{tr("Manage local model")}</Link>
            {!provider.publication_enabled ? <button type="button" className="rf-btn rf-btn-sm" disabled={busy} onClick={() => confirm({ title: tr("Enable friend AI sharing?"),
              body: tr("Publishes this service's model and capacity metadata to the network. Only explicitly authorized friends can submit requests. Prompts and model files are not published."),
              risk: "medium", confirmLabel: tr("Enable friend sharing"), onConfirm: async () => {
                working.current = true; setBusy(true); try { await client.publishLLMService({ network_id: provider.network_id, benchmark: false }); await load(); }
                finally { working.current = false; setBusy(false); }
              } })}>{tr("Enable friend AI sharing")}</button> : null}
          </div> : <p className="rq-ai-row">{tr("No local AI service configured.")} <Link className="rf-lnk rf-lnk-sm" to="/services/manage">{tr("Set up local AI")}</Link></p>}
          {active.length === 0 ? <p className="rq-ai-row rq-none-text">{tr("Add a friend to share AI.")}</p> : shown.map((friend) => {
            const grant = grants.find((row) => row.relationship_id === friend.relationship_id && row.service_id === service);
            const allowed = grant?.effective === true;
            return <div key={friend.relationship_id} className="rq-ai-row rq-ai-grant" aria-label={tr("AI with {{name}}", { name: friend.node_name })} role="group">
              {service ? <>
                <div className="rq-ai-main">
                  <b>{tr("{{name}} can use {{model}}", { name: friend.node_name, model: provider?.service?.model_alias ?? "" })}</b>
                  <small>{allowed ? tr("Running work may continue briefly after you revoke.") : tr("Allow only while friend sharing is enabled. Other services stay unchanged.")}</small>
                </div>
                <span className="rf-sr">{tr("Your {{model}}: {{state}}", { model: provider?.service?.model_alias ?? "", state: allowed ? tr("Allowed") : tr("Not allowed") })}</span>
                <span aria-hidden="true"><Tinted tone={allowed ? "self" : "off"} className="rq-status"><i />{allowed ? tr("Allowed") : tr("Not allowed")}</Tinted></span>
                <button type="button" className={`rf-btn rf-btn-sm${allowed ? " rf-btn-d" : " rf-btn-p"}`} disabled={busy || (!allowed && (serviceChanged || !pricing || !provider?.publication_enabled))} onClick={() => confirm({ title: allowed ? tr("Revoke AI access for {{name}}?", { name: friend.node_name }) : tr("Allow AI access for {{name}}?", { name: friend.node_name }),
                  body: allowed ? tr("Blocks new requests immediately. Running work will be asked to cancel, but computation may not stop immediately.")
                    : tr("Allows {{name}} to use {{model}} while friend sharing is enabled. Other services remain unchanged.", { name: friend.node_name, model: provider?.service?.model_alias ?? "" }),
                  risk: "medium", confirmLabel: allowed ? tr("Revoke access") : tr("Allow access"),
                  onConfirm: () => change(friend, service, !allowed, grant) })}>{allowed ? tr("Revoke AI access") : tr("Allow AI access")}</button>
              </> : null}
              {grants.filter((row) => row.relationship_id === friend.relationship_id && row.service_id !== service && row.allowed).map((old) =>
                <div key={old.service_id} className="rq-ai-old"><span>{tr("Previous service: {{id}}", { id: old.service_id })}</span> <button type="button" className="rf-btn rf-btn-sm" disabled={busy} onClick={() => void change(friend, old.service_id, false, old)}>{tr("Revoke {{id}}", { id: old.service_id })}</button></div>)}
            </div>;
          })}
        </div>
      </section>
      {shown.map((friend) => {
        const snapshot = remote.find((row) => row.relationship_id === friend.relationship_id);
        return <section key={friend.relationship_id} className="rq-ai-sec" aria-label={tr("AI with {{name}}", { name: friend.node_name })}>
          <div className="rq-ai-sec-head">
            <h3>{tr("{{name}}'s AI", { name: friend.node_name })}</h3>
            <button type="button" className="rf-btn rf-btn-sm" disabled={busy} onClick={() => void refresh(friend)}>{tr("Check {{name}}'s AI", { name: friend.node_name })}</button>
          </div>
          <div className="rq-box">
            {!snapshot ? <div className="rq-ai-row"><div className="rq-ai-main"><b>{tr("Friend AI access has not been checked.")}</b><small>{tr("Check to see what {{name}} has allowed.", { name: friend.node_name })}</small></div></div> : <>
              {snapshot.services.length === 0 ? <div className="rq-ai-row"><div className="rq-ai-main"><b>{snapshot.status === "revoked" ? tr("Your friend revoked AI access. Ask them before requesting a new grant.")
                : snapshot.status === "not_authorized" ? tr("Your friend has not allowed you to use their AI. Ask them to review your permission.")
                : snapshot.status === "stale" ? tr("Permission status expired. Refresh to check again.")
                : tr("Your friend's model is unavailable. Ask them to check its setup.")}</b></div></div> : snapshot.services.map((record) => {
                const age = Date.now() / 1000 - snapshot.checked_at;
                const stale = snapshot.status === "stale" || age < 0 || age > 120;
                const usable = !stale && record.online && record.capacity?.available !== 0;
                return <div key={record.service.package_id} className="rq-ai-row">
                  <div className="rq-ai-main"><b>{record.service.model_alias} · {stale ? tr("Status expired — refresh") : record.capacity?.available === 0 ? tr("Busy — wait and refresh") : record.online ? tr("Ready") : tr("Not ready or sharing paused")}</b></div>
                  {usable ? <Link className="rf-btn rf-btn-sm" to={`/ask?${new URLSearchParams({ peer: friend.peer_id, service: record.service.package_id, network: record.network_id ?? "rynmesh-main" })}`}>{tr("Ask {{name}}'s AI", { name: friend.node_name })}<ArrowRight size={13} strokeWidth={2} aria-hidden="true" /></Link> : null}
                </div>;
              })}
              <p className="rq-ai-row rq-ai-checked"><small>{tr("Last checked {{time}}", { time: relativeAgo(snapshot.checked_at * 1000, Date.now(), uiLocale()) ?? formatWhen(snapshot.checked_at * 1000) })}{snapshot.status === "stale" ? tr(" · Refresh before using") : ""}</small></p>
            </>}
          </div>
        </section>;
      })}
    </>}
  </div>;
}
