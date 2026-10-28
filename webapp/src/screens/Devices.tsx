import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Check, CircleAlert, Clock, Eye, Info, Link2, Pause, Play, Plus, RefreshCw, ShieldCheck, TriangleAlert } from "lucide-react";
import { useAppContext } from "../appContext";
import ReadingSyncConflicts from "../components/ReadingSyncConflicts";
import DeviceSyncHealth from "../components/DeviceSyncHealth";
import { runThenReload } from "../domain/actThenReload";
import { captureFailureReason, deviceSyncApi, pairLabels, quarantineReason, scopeNames, syncScopes } from "../domain/deviceSync";
import type { DeviceIdentity, DeviceInvite, DevicePair, DeviceStatus, SyncScope } from "../domain/deviceSync";
import DeviceErasurePanel from "../components/DeviceErasurePanel";
import { tr, useUILanguage } from "../uiI18n";
import { DeviceTile, Pill, deviceHue, shortIdentity, type Tone } from "../personal/drop/FamilyParts";
import "./device-sync-refresh.css";

const scopeHint = (scope: SyncScope) => ({
  bookmarks: tr("Bookmarks and saved items"), reading: tr("Where you stopped in each article"), conversations: tr("Your conversations with Ryn"),
})[scope];
/** Wire-format example, not copy: kept out of JSX so the copy lint does not treat it as text. */
const INVITE_PLACEHOLDER = "ryn-device://invite/…";
const hostOf = (endpoint: string) => endpoint.replace(/^[a-z]+:\/\//i, "").replace(/\/$/, "");

type ScopeVariant = "cards" | "chips" | "list";

function ScopeChoice({ value, onChange, allowed = syncScopes, disabled = false, label, variant }: {
  value: SyncScope[]; onChange: (value: SyncScope[]) => void; allowed?: SyncScope[]; disabled?: boolean; label: string; variant: ScopeVariant;
}) {
  const uid = useId();
  return <fieldset className={`rf-scopes rf-scopes-${variant}`} disabled={disabled}><legend className="rf-legend">{label}</legend>
    <div className="rf-scope-grid">{syncScopes.map((scope) => {
      const off = !allowed.includes(scope);
      const id = `${uid}${scope}`;
      return <label key={scope} className={variant === "chips" ? "rf-chip" : variant === "cards" ? "rf-opt" : "rf-check"}>
        <input type="checkbox" checked={value.includes(scope)} disabled={off} aria-labelledby={`${id}n`} aria-describedby={variant === "chips" ? undefined : `${id}d`}
          onChange={(event) => onChange(event.target.checked ? [...value, scope].sort() : value.filter((item) => item !== scope))} />
        {variant === "chips" ? <><Check className="rf-on" size={13} strokeWidth={2.2} aria-hidden="true" /><Plus className="rf-plus" size={13} strokeWidth={2} aria-hidden="true" /><span id={`${id}n`}>{scopeNames[scope]}</span></>
          : <span className="rf-opt-stack"><b id={`${id}n`}>{scopeNames[scope]}</b>
            {variant === "cards" ? <small id={`${id}d`}>{scopeHint(scope)}</small> : off ? <small id={`${id}d`}>{tr("Not offered")}</small> : null}</span>}
      </label>;
    })}</div>
  </fieldset>;
}

function Identity({ device }: { device: DeviceIdentity }) {
  return <dl className="rf-identity"><div><dt>Device</dt><dd>{device.name}</dd></div><div><dt>Identity</dt><dd className="rf-mono rf-ell" title={device.actor}>{shortIdentity(device.actor, "actor:")}</dd></div>
    <div><dt>Address</dt><dd className="rf-mono">{hostOf(device.endpoint)}</dd></div></dl>;
}

function Hint({ icon, children }: { icon?: "info" | "eye"; children: React.ReactNode }) {
  return <p className="rf-note">{icon === "eye" ? <Eye size={14} aria-hidden="true" /> : <Info size={14} aria-hidden="true" />}<span>{children}</span></p>;
}

function PairCard({ pair, busy, act }: { pair: DevicePair; busy: boolean; act: (operation: () => Promise<unknown>) => Promise<void> }) {
  const { confirm } = useAppContext();
  const [scopes, setScopes] = useState<SyncScope[]>(pair.status === "awaiting_owner" ? [] : pair.scopes);
  const [enteredCode, setEnteredCode] = useState("");
  // The approving device must never display the code it is asking for: showing
  // it here would turn the check into a copy from this same screen. The owner
  // reads it off the joining device, and the node decides whether it matches.
  const approving = pair.status === "awaiting_owner";
  const active = pair.status === "active";
  const rejected: Partial<Record<SyncScope, number>> = pair.sync?.rejected_by_peer ?? {};
  const refused = syncScopes.filter((scope) => rejected[scope]);
  const sync = pair.sync;
  const hasRefusals = refused.length > 0 || Boolean(sync?.rejected_details_truncated);
  // Card tone and pill: derived from the same status the card already reports, never from a clock or a guess.
  const tone: Tone = !active ? (approving ? "self" : pair.status === "revoked" || pair.status === "expired" || pair.status === "rejected" ? "off" : "warn")
    : !sync ? "ok" : sync.conflicts > 0 || hasRefusals ? "warn" : sync.state === "confirmed" || sync.state === "no_scope" ? "ok" : sync.state === "paused" ? "off" : "off";
  const pill = active && sync
    ? (sync.conflicts > 0 ? tr("Conflicts") : sync.state === "paused" || pair.paused || pair.remote_paused ? tr("Paused") : sync.state === "confirmed" ? tr("Syncing") : tr("Not confirmed"))
    : pairLabels[pair.status] ?? "Status unavailable";
  const dimmed = !active || tone === "off";
  const changed = syncScopes.some((scope) => scopes.includes(scope) !== pair.scopes.includes(scope));
  const mutual = pair.effective_scopes.map((scope) => scopeNames[scope]).join(", ") || "None";
  return <article id={`device-${pair.id}`} className={`rf-pc rf-h-${deviceHue(pair.device.peer_id || pair.id)}${approving ? " review" : ""}`} data-off={dimmed ? "true" : "false"} aria-label={`Device ${pair.device.name}`}>
    <div className="rf-pc-head">
      <DeviceTile kind="desktop" hue={deviceHue(pair.device.peer_id || pair.id)} off={dimmed && !approving} />
      <div className="rf-pc-id"><h3>{pair.device.name}</h3>
        <p className="rf-ell rf-mono-sub">{approving ? tr("Wants to pair with this computer") : `${shortIdentity(pair.device.actor, "actor:")} · ${hostOf(pair.device.endpoint)}`}</p></div>
      <Pill tone={tone}>{pill}</Pill>
    </div>
    <div className="rf-pc-body">
      {pair.role === "joiner" && pair.status !== "revoked" && pair.verification_code ? <p className="rf-pc-code">Enter this code on the computer that sent the invitation: <strong className="rf-mono">{pair.verification_code}</strong></p> : null}
      {approving ? <>
        <p className="rf-pc-line">Only approve a computer you own. Review its identity and choose what may sync in both directions.</p>
        <Identity device={pair.device} />
        <ScopeChoice label="Allow on this device" value={scopes} onChange={setScopes} allowed={pair.scopes} disabled={busy} variant="list" />
        <div className="rf-code-row">
          <label className="rf-field"><span>Enter the code shown on the other device</span><input className="rf-inp rf-code-in rf-mono" type="text" inputMode="numeric" autoComplete="off" placeholder="000 000" value={enteredCode} disabled={busy}
            onChange={(event) => setEnteredCode(event.target.value)} /></label>
          <button type="button" className="rf-btn rf-btn-p rf-btn-lg" disabled={busy || enteredCode.trim() === ""} onClick={() => void act(() => deviceSyncApi.approve(pair, scopes, enteredCode))}><Check size={15} strokeWidth={2} aria-hidden="true" />Approve this device</button>
        </div>
        <Hint icon="eye">Read the code from the other computer's My devices screen and type it here. It is not shown on this screen.</Hint>
      </> : null}
      {active ? <>
        {sync ? <div role="status" className={`rf-status rf-t-${tone}`}>
          <span className="rf-status-ico" aria-hidden="true">{tone === "ok" ? <Check size={13} strokeWidth={2.2} /> : tone === "warn" ? <CircleAlert size={13} strokeWidth={2.2} /> : <RefreshCw size={13} strokeWidth={2.2} />}</span>
          <div className="rf-status-text">
            <p><b>{(refused.length > 0 || sync.rejected_details_truncated) && sync.state === "confirmed" ? "Remaining local changes confirmed by the other device."
              : ({ confirmed: "Selected local changes confirmed by the other device.", pending: "Changes are waiting for confirmation.",
              waiting: "Last transfer was not confirmed. Reconnect and retry.", failed: "Local sync storage is unavailable. Free space or check storage, then retry.",
              paused: "Content transfer is paused.", no_scope: "No category is currently allowed by both devices.",
              conflict: "Changes from different devices need review. Both versions have been kept." } as Record<string, string>)[sync.state] ?? "Checking sync status."}</b></p>
            {sync.pending !== null ? <p>{sync.pending} local changes waiting for confirmation.</p> : null}
            {sync.last_success_at !== null ? <p>Last confirmation across selected categories: {new Date(sync.last_success_at * 1000).toLocaleString()}.</p> : null}
            {sync.conflicts > 0 ? <p>{sync.conflicts} unresolved conflicts. <a className="rf-lnk" href="#reading-sync-conflicts">Review reading choices below</a>; review conversation branches in <Link className="rf-lnk" to="/ask">Ask Ryn</Link>.</p> : null}
            {refused.map((scope) => <p key={scope}>{rejected[scope]} {rejected[scope] === 1 ? "record" : "records"} in {scopeNames[scope]} could
              not be merged by {pair.device.name}. They will be sent again after they change on this device.</p>)}
            {sync.rejected_details_truncated ? <p>Additional rejected records are not listed because diagnostic storage reached its limit. The displayed counts are minimum counts; a cleared list does not confirm every record was merged.</p> : null}
          </div>
        </div> : null}
        <p className="rf-pc-line">{pair.paused ? "Paused on this device." : pair.remote_paused ? "Paused on the other device." : "Both devices have confirmed the pairing."}</p>
        <ScopeChoice label="Your allowed scope" value={scopes} onChange={setScopes} disabled={busy} variant="chips" />
      </> : null}
      {pair.status === "revoked" ? <p className="rf-pc-line">{pair.removal_pending ? "Removed locally; waiting to notify the other device." : "Removal confirmed by the other device, or initiated there."} Existing copies cannot be recalled.</p> : null}
    </div>
    <div className="rf-pc-foot">
      {active ? <p className="rf-pc-mutual">Mutually allowed: {mutual}.</p> : <span className="rf-pc-mutual" />}
      {active && changed ? <button type="button" className="rf-btn rf-btn-p rf-btn-sm" disabled={busy} onClick={() => void act(() => deviceSyncApi.configure(pair, scopes, pair.paused))}>Save scope</button> : null}
      {pair.status !== "revoked" || pair.removal_pending ? <button type="button" className="rf-btn rf-btn-sm" disabled={busy} onClick={() => void act(() => deviceSyncApi.retry(pair.id))}><RefreshCw size={14} strokeWidth={1.8} aria-hidden="true" />Retry connection</button> : null}
      {active ? <button type="button" className="rf-btn rf-btn-sm" disabled={busy} onClick={() => void act(() => deviceSyncApi.configure(pair, pair.scopes, !pair.paused))}>{pair.paused ? <Play size={14} strokeWidth={1.8} aria-hidden="true" /> : <Pause size={14} strokeWidth={1.8} aria-hidden="true" />}{pair.paused ? "Resume" : "Pause"}</button> : null}
      {pair.status !== "revoked" ? <button type="button" className="rf-btn rf-btn-sm rf-btn-d" disabled={busy} onClick={() => confirm({ title: `Remove ${pair.device.name}?`, risk: "high", confirmLabel: "Remove device",
        body: "This computer will reject further sync immediately. A transfer already in flight may finish on the other device, and the removal notice may arrive later. Copies already there cannot be recalled. Reconnecting requires a new invitation and approval.",
        onConfirm: () => act(() => deviceSyncApi.remove(pair)) })}>Remove device</button> : null}
    </div>
  </article>;
}

export default function Devices() {
  useUILanguage();
  const [status, setStatus] = useState<DeviceStatus | null>(null);
  const [readAt, setReadAt] = useState<number | null>(null);
  const [statusFailed, setStatusFailed] = useState(false);
  const [refreshError, setRefreshError] = useState("");
  const [now, setNow] = useState(Date.now);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [offered, setOffered] = useState<SyncScope[]>([]);
  const [invite, setInvite] = useState<{ uri: string; invite: DeviceInvite } | null>(null);
  const [paste, setPaste] = useState("");
  const [review, setReview] = useState<{ uri: string; invite: DeviceInvite } | null>(null);
  const [selected, setSelected] = useState<SyncScope[]>([]);
  const [checked, setChecked] = useState(false);
  const [tab, setTab] = useState<"invite" | "use">("invite");
  const revision = useRef(0);
  const mounted = useRef(true);
  const loadVersion = useRef(0);
  const load = useCallback(async () => {
    const version = ++loadVersion.current;
    let result: DeviceStatus;
    try { result = await deviceSyncApi.status(); }
    catch (cause) {
      if (mounted.current && version === loadVersion.current) setStatusFailed(true);
      throw cause;
    }
    if (!mounted.current || version !== loadVersion.current) return;
    setStatus(result);
    setReadAt(Date.now());
    setStatusFailed(false);
    setRefreshError("");
    setInvite((prior) => prior && result.invites.some((row) => row.id === prior.invite.id && row.status === "open") ? prior : null);
  }, []);
  const act = async (operation: () => Promise<unknown>) => {
    setBusy(true); setError(""); setNotice("");
    await runThenReload(operation, load, {
      onError: (cause) => { if (mounted.current) setError(cause instanceof Error ? cause.message : "Could not confirm this operation. Refresh and retry."); },
      onNotice: (message) => { if (mounted.current) setNotice(message); },
    });
    if (mounted.current) setBusy(false);
  };
  // Refreshing performs no operation on the node, so it must not borrow act()'s
  // "Done on the node" notice: a failed reload here is simply a failed reload.
  const refresh = async () => {
    setBusy(true); setError(""); setNotice("");
    try { await load(); }
    catch { if (mounted.current) setRefreshError("Could not refresh device status. Check your connection and refresh."); }
    if (mounted.current) setBusy(false);
  };
  useEffect(() => {
    mounted.current = true;
    let polling = false;
    const poll = async () => {
      if (polling) return;
      polling = true;
      try { await load(); }
      catch { if (mounted.current) setRefreshError("Could not refresh device status. Check your connection and refresh."); }
      finally { polling = false; }
    };
    void poll();
    const timer = window.setInterval(() => { setNow(Date.now()); void poll(); }, 5000);
    return () => { mounted.current = false; window.clearInterval(timer); };
  }, [load]);
  const minutesLeft = invite ? Math.max(0, Math.ceil((invite.invite.expires * 1000 - now) / 60000)) : 0;
  const openInvites = status?.invites.filter((row) => row.status === "open" && row.id !== invite?.invite.id) ?? [];
  const hasActive = Boolean(status?.devices.some((pair) => pair.status === "active"));
  return <div className="rf rd-sync">
    <header className="rd-head">
      <div className="rd-head-text"><h1>{tr("Device sync")}</h1><p>Pair computers you own. Review both identities and choose each category before allowing sync.</p></div>
      <div className="rd-head-actions">
        <button type="button" className="rf-btn" disabled={busy} onClick={() => void refresh()}><RefreshCw size={15} strokeWidth={1.7} aria-hidden="true" />Refresh devices</button>
        <a className="rf-btn rf-btn-p" href="#add-computer" onClick={() => setTab("invite")}><Plus size={15} strokeWidth={1.9} aria-hidden="true" />{tr("Invite a computer")}</a>
      </div>
    </header>
    <DeviceSyncHealth status={status} readAt={readAt} stale={statusFailed || (readAt !== null && now - readAt >= 15000)} />
    {status && !status.data_transfer_available ? <div className="rf-callout"><Info size={16} aria-hidden="true" /><p>Device pairing is available in this development build. Personal data transfer is still being connected; a confirmed pairing does not mean your content has synced.</p></div> : null}
    {error || refreshError ? <div role="alert" className="rf-callout rf-callout-err"><CircleAlert size={16} aria-hidden="true" /><p>{error || refreshError}</p></div> : null}
    {notice ? <div role="status" className="rf-callout rf-callout-ok"><Check size={16} aria-hidden="true" /><p>{notice}</p></div> : null}
    {status && !status.pairing_available ? <div role="status" className="rf-callout rf-callout-warn"><TriangleAlert size={16} aria-hidden="true" /><p>A reachable address is needed for new invitations. Check <Link className="rf-lnk" to="/settings">Network settings</Link>. Existing devices can still be removed.</p></div> : null}
    {status && status.capture_failures.count > 0 ? <div role="status" className="rf-callout rf-callout-warn"><TriangleAlert size={16} aria-hidden="true" /><p>{status.capture_failures.count} local {status.capture_failures.count === 1 ? "change" : "changes"} could
      not be queued for sync ({captureFailureReason(status.capture_failures.codes)}). They stay on this device.</p></div> : null}
    {status && status.quarantined_count > 0 ? <div role="status" className="rf-callout rf-callout-warn"><TriangleAlert size={16} aria-hidden="true" /><p>{status.quarantined_count} local {status.quarantined_count === 1 ? "record" : "records"} could
      not be merged into the sync replica on this device ({quarantineReason(status.quarantined)}). The original records remain on this device. Review the affected bookmarks, reading records or conversations before resolving them.</p></div> : null}
    <div className="rd-cols">
      <section className="rd-devs" aria-labelledby="paired-title">
        <div className="rd-sec-head"><h2 id="paired-title">Your devices</h2>{hasActive ? <span>Turning a category off stops future transfers. Copies already on either computer stay there.</span> : null}</div>
        {!status ? <p className="rf-muted">Loading devices…</p> : !status.devices.length ? <div className="rf-empty">
          <span className="rf-empty-art" aria-hidden="true"><i className="rf-h-indigo" /><i className="rf-empty-ghost" /><b /></span>
          <h3>No paired devices yet.</h3><p>{tr("Pair a computer to see its sync progress here.")}</p>
          <a className="rf-btn rf-btn-p rf-btn-sm" href="#add-computer" onClick={() => setTab("invite")}><Plus size={14} strokeWidth={1.9} aria-hidden="true" />{tr("Invite a computer")}</a>
        </div> : status.devices.map((pair) =>
          <PairCard key={`${pair.id}:${pair.revision}:${pair.status}`} pair={pair} busy={busy} act={act} />)}
      </section>

      <aside id="add-computer" className="rf-card rd-pair" aria-labelledby="add-title">
        <div className="rd-pair-top">
          <div className="rd-pair-title"><span className="rd-pair-mark" aria-hidden="true"><Link2 size={18} strokeWidth={1.8} /></span>
            <div><h2 id="add-title">{tr("Pair a computer")}</h2><p>{tr("Nothing syncs until both computers approve.")}</p></div></div>
          <div className="rf-seg rf-seg-fill" role="group" aria-label={tr("Pairing method")}>
            <button type="button" aria-pressed={tab === "invite"} onClick={() => setTab("invite")}>{tr("Create invite")}</button>
            <button type="button" aria-pressed={tab === "use"} onClick={() => setTab("use")}>{tr("Use an invite")}</button>
          </div>
        </div>
        <div className="rf-pane" data-on={tab === "invite"}>
          <p className="rf-pane-lead">One use, valid for 15 minutes. Choose the categories you want to allow in both directions; nothing is selected automatically.</p>
          <ScopeChoice label="Offer to sync" value={offered} onChange={setOffered} disabled={busy || Boolean(invite)} variant="cards" />
          {!invite ? <button type="button" className="rf-btn rf-btn-p rf-btn-lg rf-btn-block" disabled={busy || !status?.pairing_available} onClick={() => void act(async () => setInvite(await deviceSyncApi.invite(offered)))}>Create device invite</button> : <div className="rf-invite">
            <div className="rf-invite-head"><label htmlFor="device-invite-text">Device invitation to copy</label>
              <span className="rf-pill rf-pill-sm rf-t-warn"><Clock size={12} strokeWidth={2} aria-hidden="true" />{tr("{{count}} min left", { count: minutesLeft })}</span></div>
            <textarea id="device-invite-text" className="rf-inp rf-invite-text rf-mono" readOnly rows={3} value={invite.uri} />
            <div className="rf-row"><button type="button" className="rf-btn rf-btn-p" disabled={busy} onClick={() => void act(async () => {
              try { await navigator.clipboard.writeText(invite.uri); setNotice("Device invitation copied."); }
              catch { throw new Error("Clipboard unavailable. Select and copy the invitation text above."); }
            })}>Copy device invite</button><button type="button" className="rf-btn" disabled={busy} onClick={() => void act(async () => { await deviceSyncApi.cancel(invite.invite.id); setInvite(null); })}>Cancel invitation</button></div>
            <p className="rf-hint">Expires {new Date(invite.invite.expires * 1000).toLocaleString()}. On the other computer, open My devices and paste this invitation.</p>
          </div>}
        </div>
        <div className="rf-pane" data-on={tab === "use"}>
          <label className="rf-field"><span>Paste device invitation</span>
            <textarea className="rf-inp rf-invite-text rf-mono" rows={3} placeholder={INVITE_PLACEHOLDER} value={paste} onChange={(event) => {
              revision.current += 1; setPaste(event.target.value); setReview(null); setSelected([]); setChecked(false);
            }} /></label>
          {!review ? <button type="button" className="rf-btn rf-btn-p rf-btn-lg rf-btn-block" disabled={busy || !paste.trim()} onClick={() => void act(async () => {
            const version = revision.current, uri = paste.trim();
            const preview = await deviceSyncApi.inspect(uri);
            if (version === revision.current) setReview({ uri, invite: preview });
          })}>Review device invite</button> : <>
            <div className="rf-review">
              <div className="rf-review-who"><DeviceTile kind="desktop" hue={deviceHue(review.invite.device.peer_id || review.invite.id)} size="sm" />
                <div><b>{review.invite.device.name}</b><small className="rf-mono rf-ell">{shortIdentity(review.invite.device.actor, "actor:")} · {hostOf(review.invite.device.endpoint)}</small></div></div>
              <p className="rf-review-ok"><ShieldCheck size={14} strokeWidth={1.8} aria-hidden="true" /><span>Signature checked locally. No personal data has been sent. Expires {new Date(review.invite.expires * 1000).toLocaleString()}.</span></p>
              <ScopeChoice label="Choose for this computer" value={selected} onChange={setSelected} allowed={review.invite.scopes} disabled={busy} variant="list" />
              <p className="rf-hint">These categories may sync in both directions after the other computer also approves.</p>
            </div>
            <label className="rf-check"><input type="checkbox" checked={checked} disabled={busy} onChange={(event) => setChecked(event.target.checked)} />I checked this invitation is from my own computer</label>
            <button type="button" className="rf-btn rf-btn-p rf-btn-lg rf-btn-block" disabled={busy || !checked || !status?.pairing_available} onClick={() => void act(async () => {
              const version = revision.current;
              await deviceSyncApi.join(review.uri, selected);
              if (version === revision.current) { setPaste(""); setReview(null); setChecked(false); setSelected([]); }
              setNotice("Request saved. Compare the codes on both computers and approve on the computer that sent the invitation.");
            })}>Request pairing</button>
          </>}
        </div>
        {openInvites.length ? <div className="rd-pair-foot">{openInvites.map((row) => <div key={row.id} className="rd-pair-open">
          <Clock size={15} strokeWidth={1.7} aria-hidden="true" />
          <p>Open invitation · expires {new Date(row.expires * 1000).toLocaleString()}. Invitation text is only shown when created; cancel this invitation if you no longer have it.</p>
          <button type="button" className="rf-btn rf-btn-sm" disabled={busy} onClick={() => void act(() => deviceSyncApi.cancel(row.id))}>Cancel unused invitation</button>
        </div>)}</div> : null}
      </aside>
    </div>
    {status?.data_transfer_available ? <ReadingSyncConflicts devices={status.devices} onResolved={load} /> : null}
    {status ? <DeviceErasurePanel devices={status.devices} /> : null}
    <p className="rf-note rd-foot-note"><Info size={13} strokeWidth={1.8} aria-hidden="true" /><span>Device pairing is separate from <Link className="rf-lnk" to="/friends">Friends</Link>. It does not grant AI access or copy models, downloaded articles, credentials or friend permissions.</span></p>
  </div>;
}
