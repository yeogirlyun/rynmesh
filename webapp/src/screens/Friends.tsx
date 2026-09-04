import { Check, Copy, Link2, MessageCircle, QrCode, RefreshCw, ShieldCheck, Trash2, UserPlus } from "lucide-react";
import QRCode from "qrcode";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAppContext } from "../appContext";
import { Button, PageHeader, Panel } from "../components/ui";
import type { FriendInvitePreview, FriendInviteResult, FriendRecord } from "../domain/friendTypes";
import styles from "./Friends.module.css";

export default function Friends() {
  const { client, confirm, notify } = useAppContext();
  const navigate = useNavigate();
  const [friends, setFriends] = useState<FriendRecord[]>([]);
  const [invite, setInvite] = useState<FriendInviteResult | null>(null);
  const [qr, setQr] = useState("");
  const [paste, setPaste] = useState("");
  const [preview, setPreview] = useState<FriendInvitePreview | null>(null);
  const [busy, setBusy] = useState(false);

  const load = async () => setFriends(await client.listFriends());
  useEffect(() => { void load(); }, [client]);

  const createInvite = async () => {
    setBusy(true);
    try {
      const value = await client.createFriendInvite(15);
      setInvite(value);
      setQr(await QRCode.toDataURL(value.invite_uri, { width: 320, margin: 1, color: { dark: "#07130e", light: "#ffffff" } }));
    } finally { setBusy(false); }
  };

  const inspect = async () => {
    setBusy(true); setPreview(null);
    try { setPreview(await client.inspectFriendInvite(paste.trim())); }
    catch (cause) { notify("danger", cause instanceof Error ? cause.message : "This invite is invalid or expired"); }
    finally { setBusy(false); }
  };

  const join = async () => {
    setBusy(true);
    try {
      await client.joinFriend(paste.trim());
      setPaste(""); setPreview(null); await load(); notify("ok", "Friend added — you can message each other now");
    } catch (cause) { notify("danger", cause instanceof Error ? cause.message : "Could not add friend"); }
    finally { setBusy(false); }
  };

  const revoke = (friend: FriendRecord) => confirm({
    title: `Remove ${friend.node_name}?`,
    body: "Messaging stops immediately on this device. A newly discovered node cannot restore this relationship.",
    confirmLabel: "Remove friend",
    risk: "high",
    onConfirm: async () => { await client.revokeFriend(friend.relationship_id); await load(); notify("ok", "Friend access removed"); },
  });

  return <div className="screen-stack">
    <PageHeader eyebrow="Private sharing" title="Friends" context="Pair deliberately, then message or share a content card. Discovery alone never makes someone a friend." />
    <div className={styles.heroGrid}>
      <Panel className={styles.actionCard}>
        <span className="eyebrow"><QrCode size={14} /> Invite a friend</span>
        <h3>Create a one-time invite</h3>
        <p className="muted">Valid for 15 minutes and usable once. It grants only messages, attachments up to 5 MiB, and content cards.</p>
        {!invite ? <Button variant="primary" icon={UserPlus} disabled={busy} onClick={() => void createInvite()}>Create invite</Button> :
          <div className={styles.inviteBox}>
            {qr ? <img className={styles.qr} src={qr} alt="Scannable friend invite QR code" /> : null}
            <div><strong>Scan or copy</strong><p className="muted">Expires {new Date(invite.invite.expires_at).toLocaleTimeString()} · one use only</p><code className={styles.code}>{invite.invite_uri}</code><div className={styles.choiceRow}><Button icon={Copy} onClick={() => void navigator.clipboard.writeText(invite.invite_uri).then(() => notify("ok", "Invite copied"))}>Copy invite</Button><Button variant="danger" onClick={() => void client.cancelFriendInvite(invite.invite.invite_id).then(() => { setInvite(null); setQr(""); notify("ok", "Invite cancelled"); })}>Cancel invite</Button></div></div>
          </div>}
      </Panel>
      <Panel className={styles.actionCard}>
        <span className="eyebrow"><Link2 size={14} /> Use an invite</span>
        <h3>Paste, review, then connect</h3>
        <textarea className={styles.paste} aria-label="Friend invite" value={paste} onChange={(event) => { setPaste(event.target.value); setPreview(null); }} placeholder="Paste a rynmesh:// friend invite" />
        {!preview ? <Button icon={ShieldCheck} disabled={busy || !paste.trim()} onClick={() => void inspect()}>Review invite</Button> : <div className={styles.review}>
          <strong><Check size={15} /> Signed invite verified locally</strong>
          <dl><dt>Device</dt><dd>{preview.node_name}</dd><dt>Identity</dt><dd>{preview.fingerprint ?? preview.peer_id}</dd><dt>Network</dt><dd>{preview.network_id ?? "rynmesh-main"}</dd><dt>Endpoint</dt><dd>{preview.endpoint} · {preview.address_category ?? "direct"}</dd><dt>Expires</dt><dd>{new Date(preview.expires_at).toLocaleString()}</dd><dt>Can do</dt><dd>{preview.permissions.join(" · ")}</dd><dt>Cannot do</dt><dd>AI, VPN, files, trust, credits</dd></dl>
          <Button variant="primary" icon={UserPlus} disabled={busy} onClick={() => void join()}>Add this friend</Button>
        </div>}
      </Panel>
    </div>
    <Panel>
      <span className="eyebrow">Your relationships</span><h2>Active friends</h2>
      <div className={styles.friendList}>{friends.filter((friend) => friend.status === "active").length === 0 ? <div className={styles.empty}>No active friends yet. Create or paste an invite above.</div> : friends.filter((friend) => friend.status === "active").map((friend) => <div className={styles.friendCard} key={friend.relationship_id}>
        <div className={styles.friendIdentity}><span className={styles.avatar}>{friend.node_name.slice(0, 1).toUpperCase()}</span><div><h3>{friend.node_name}</h3><div className={styles.meta}>{friend.endpoint}</div><div>{friend.permissions.map((permission) => <span className={styles.permission} key={permission}>{permission}</span>)}</div></div></div>
        <div className={styles.choiceRow}><Button icon={MessageCircle} onClick={() => navigate(`/chat?friend=${encodeURIComponent(friend.peer_id)}`)}>Message</Button><Button variant="danger" icon={Trash2} onClick={() => revoke(friend)}>Remove</Button></div>
      </div>)}</div>
      {friends.some((friend) => friend.status === "revoked" && friend.revocation_delivery === "pending") ? <><h3>Removal waiting for remote device</h3><p className="muted">Access is already blocked locally. Retry only delivers the signed removal notice.</p>{friends.filter((friend) => friend.status === "revoked" && friend.revocation_delivery === "pending").map((friend) => <div className={styles.friendCard} key={friend.relationship_id}><span>{friend.node_name} · locally removed</span><Button icon={RefreshCw} onClick={() => void client.retryFriendRevocation(friend.relationship_id).then(load)}>Retry notice</Button></div>)}</> : null}
    </Panel>
  </div>;
}
