import { Activity } from "lucide-react";
import type { FriendDiagnostics } from "../../domain/friendsClient";
import type { FriendRecord } from "../../domain/friendTypes";
import { tr, useUILanguage } from "../../uiI18n";
import { formatWhen } from "./FriendsParts";
import { linkInfo } from "./friendLinks";
import { useFriendDiagnostics, type FriendDiagnosticsController } from "./useFriendDiagnostics";
import "../friends-refresh.css";

type Link = FriendDiagnostics["links"][number];

function LinkCard({ link, controller, running, compact }: { link: Link; controller: FriendDiagnosticsController; running: boolean; compact: boolean }) {
  const info = linkInfo(link.state);
  const checked = [
    link.checked_at ? tr("Checked {{time}}", { time: formatWhen(link.checked_at * 1000) }) : tr("No recent check"),
    link.latency_ms !== null ? tr("{{ms}} ms", { ms: link.latency_ms }) : "",
    link.stale ? tr("Older result; check again") : "",
  ].filter(Boolean).join(" · ");
  return <section className={`rq-link rf-t-${info.tone}`} aria-label={tr("Connection to {{name}}", { name: link.node_name })}>
    <span className="rq-link-ico" aria-hidden="true"><Activity size={17} strokeWidth={1.8} /></span>
    <div className="rq-link-body">
      <h3>{tr("This node → {{name}}: {{label}}", { name: link.node_name, label: info.label })}</h3>
      <small>{checked}</small>
      <p>{info.recovery}</p>
      <div className="rq-actions">
        <button type="button" className="rf-btn rf-btn-sm" disabled={controller.busy || running} onClick={() => void controller.check([link.relationship_id])}>{tr("Recheck {{name}}", { name: link.node_name })}</button>
        <button type="button" className="rf-btn rf-btn-sm" disabled={controller.busy} aria-label={tr("Retry queued deliveries to {{name}}", { name: link.node_name })} onClick={() => void controller.retryDeliveries(link.peer_id)}>
          {compact ? tr("Retry queued deliveries") : tr("Retry queued deliveries to {{name}}", { name: link.node_name })}</button>
      </div>
    </div>
  </section>;
}

/**
 * Direct-link checks. Without `focus` it is the full panel (choose up to five friends, check them, read every result);
 * with `focus` it shows one friend's result for the Friends page, whose header button and this tab share `diag`.
 */
export default function FriendConnectionDiagnostics({ friends, focus, diag }: { friends: FriendRecord[]; focus?: FriendRecord | null; diag?: FriendDiagnosticsController }) {
  useUILanguage();
  const own = useFriendDiagnostics(friends);
  const controller = diag ?? own;
  const { selected, setSelected, result, busy, error, notice } = controller;
  const running = result?.running === true;
  const pageMode = focus !== undefined;
  const focusLink: Link | null = focus ? result?.links.find((link) => link.relationship_id === focus.relationship_id)
    ?? { relationship_id: focus.relationship_id, peer_id: focus.peer_id, node_name: focus.node_name, state: "not_checked", checked_at: null, latency_ms: null, stale: false } : null;

  const status = <>
    {pageMode ? null : <>{error ? <p role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}</>}
    {running ? <p role="status" className="rq-note-warn">{tr("A transport is still finishing its check. Refresh status before starting another batch.")}</p> : null}
  </>;

  if (pageMode) {
    return <div className="rq-diag">
      {focusLink ? <LinkCard link={focusLink} controller={controller} running={running} compact /> : null}
      {status}
      {running ? <button type="button" className="rf-btn rf-btn-sm rq-self-start" disabled={busy} onClick={() => void controller.refreshStatus()}>{tr("Refresh check status")}</button> : null}
      <p className="rq-help">{tr("Checks contact this friend without sending messages or content. A failed direct check does not rule out mailbox delivery.")}</p>
    </div>;
  }

  return <div className="rq-diag">
    <h3 className="rq-h3">{tr("Connection checks")}</h3>
    <p>{tr("Check up to five direct links from this node at once. Checks contact those friends without sending messages or content.")}</p>
    <p>{tr("Links between your friends are not checked here. Run checks on each node to inspect the other directions. A failed direct check does not rule out mailbox delivery.")}</p>
    {friends.length ? <fieldset disabled={busy}><legend className="rf-legend">{tr("Friends to check")}</legend>
      <div className="rq-chips">
        {friends.map((friend) => <label key={friend.relationship_id} className="rf-chip">
          <input type="checkbox" checked={selected.includes(friend.relationship_id)}
            disabled={!selected.includes(friend.relationship_id) && selected.length >= 5}
            onChange={(event) => setSelected((current) => event.target.checked ? [...current, friend.relationship_id]
              : current.filter((id) => id !== friend.relationship_id))} />{friend.node_name}
        </label>)}
      </div>
    </fieldset> : <p>{tr("Add a friend to check a connection.")}</p>}
    <div className="rq-actions">
      <button type="button" className="rf-btn rf-btn-p rf-btn-sm" disabled={busy || !selected.length || running} onClick={() => void controller.check(selected)}>{busy ? tr("Checking…") : tr("Check selected connections")}</button>
      {result ? <button type="button" className="rf-btn rf-btn-sm" disabled={busy} onClick={() => void controller.refreshStatus()}>{tr("Refresh check status")}</button> : null}
    </div>
    {status}
    {result?.links.filter((link) => friends.some((friend) => friend.relationship_id === link.relationship_id)).map((link) =>
      <LinkCard key={link.relationship_id} link={link} controller={controller} running={running} compact={false} />)}
  </div>;
}
