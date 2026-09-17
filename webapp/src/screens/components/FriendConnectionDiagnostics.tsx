import { useEffect, useState } from "react";
import { Button, Panel } from "../../components/ui";
import { friendsApi, type FriendDiagnostics } from "../../domain/friendsClient";
import type { FriendRecord } from "../../domain/friendTypes";
import styles from "./FriendConnectionDiagnostics.module.css";

const explanations: Record<string, [string, string]> = {
  reachable: ["Verified direct link", "Your friend's node answered an authenticated check. This does not verify model availability or every service."],
  not_checked: ["Not checked", "Select this friend and check the connection."],
  timed_out: ["Check timed out", "Check that both nodes are running and the advertised address is reachable, then retry. A timeout alone cannot identify a firewall or NAT problem."],
  unreachable: ["Could not reach this node", "Check both connections and the friend's advertised address. On different networks, the address must be reachable from here. Then retry."],
  authentication_failed: ["Friendship check rejected", "Check the friendship on both devices. Access may have been revoked or restored from an older backup. Review a fresh invitation if needed."],
  identity_mismatch: ["A different node answered", "Verify your friend's current device and address. Do not send private content until the identity matches."],
  invalid_response: ["Response could not be verified", "The reply did not match this check. Update both nodes and verify the advertised address before retrying."],
  unsupported: ["This node does not support connection checks", "Update the friend's node. This result alone does not mean ordinary messaging is broken."],
  transport_unsupported: ["Transport cannot send this check", "Select a transport that supports authenticated POST requests, then retry."],
  endpoint_missing: ["No direct address", "Ask your friend to check their advertised reachable address. Mailbox delivery may still work."],
  endpoint_rejected: ["Address rejected by local policy", "Review the friend's advertised address. Diagnostics uses the same address restrictions as private sharing."],
  friend_inactive: ["Friendship changed", "Refresh your friends list and review the current relationship before retrying."],
  rate_limited: ["Too many checks", "Wait a minute, then retry this connection."],
};

export default function FriendConnectionDiagnostics({ friends }: { friends: FriendRecord[] }) {
  const [selected, setSelected] = useState<string[]>([]);
  const [result, setResult] = useState<FriendDiagnostics | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const ids = friends.map((friend) => friend.relationship_id).join(",");
  useEffect(() => { setSelected(ids ? ids.split(",").slice(0, 5) : []); }, [ids]);

  const act = async (operation: () => Promise<void>) => {
    if (busy) return;
    setBusy(true); setError(""); setNotice("");
    try { await operation(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Connection checks could not be completed. Retry."); }
    finally { setBusy(false); }
  };

  return <Panel><div className={styles.body}><h2>Connection checks</h2>
    <p>Check up to five direct links from this node at once. Checks contact those friends without sending messages or content.</p>
    <p>Links between your friends are not checked here. Run checks on each node to inspect the other directions. A failed direct check does not rule out mailbox delivery.</p>
    {friends.length ? <fieldset className={styles.selection} disabled={busy}><legend>Friends to check</legend>
      {friends.map((friend) => <label key={friend.relationship_id}>
        <input type="checkbox" checked={selected.includes(friend.relationship_id)}
          disabled={!selected.includes(friend.relationship_id) && selected.length >= 5}
          onChange={(event) => setSelected((current) => event.target.checked ? [...current, friend.relationship_id]
            : current.filter((id) => id !== friend.relationship_id))} /> {friend.node_name}
      </label>)}
    </fieldset> : <p>Add a friend to check a connection.</p>}
    <Button disabled={busy || !selected.length || result?.running === true} onClick={() => void act(async () => {
      setResult(await friendsApi.diagnose(selected));
    })}>{busy ? "Checking…" : "Check selected connections"}</Button>
    {result ? <Button disabled={busy} onClick={() => void act(async () => { setResult(await friendsApi.diagnostics()); })}>Refresh check status</Button> : null}
    {error ? <p role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
    {result?.running ? <p role="status">A transport is still finishing its check. Refresh status before starting another batch.</p> : null}
    {result?.links.filter((link) => friends.some((friend) => friend.relationship_id === link.relationship_id)).map((link) => {
      const [label, recovery] = explanations[link.state] ?? explanations.invalid_response;
      return <section className={styles.link} key={link.relationship_id} aria-label={`Connection to ${link.node_name}`}>
        <h3>This node → {link.node_name}: {label}</h3>
        <p>{link.checked_at ? `Checked ${new Date(link.checked_at * 1000).toLocaleString()}` : "No recent check"}
          {link.latency_ms !== null ? ` · ${link.latency_ms} ms` : ""}{link.stale ? " · Older result; check again" : ""}</p>
        <p>{recovery}</p>
        <Button disabled={busy || result.running} onClick={() => void act(async () => {
          setResult(await friendsApi.diagnose([link.relationship_id]));
        })}>Recheck {link.node_name}</Button>
        <Button disabled={busy} onClick={() => void act(async () => {
          await friendsApi.retry(link.peer_id);
          setNotice("Queued delivery retry completed. Open the conversation to check its delivery receipts.");
        })}>Retry queued deliveries to {link.node_name}</Button>
      </section>;
    })}
  </div></Panel>;
}
