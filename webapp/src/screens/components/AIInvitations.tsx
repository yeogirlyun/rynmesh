import QRCode from "qrcode";
import { useEffect, useRef, useState } from "react";
import { useAppContext } from "../../appContext";
import { Button } from "../../components/ui";
import { friendsApi, installationLink, invitationText } from "../../domain/friendsClient";
import type { FriendInvitePreview, FriendInviteResult } from "../../domain/friendTypes";

export default function AIInvitations({ serviceId, disabled }: { serviceId: string; disabled: boolean }) {
  const { confirm } = useAppContext();
  const [created, setCreated] = useState<FriendInviteResult | null>(null);
  const [invites, setInvites] = useState<(FriendInvitePreview & { status: string })[]>([]);
  const [qr, setQr] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const alert = useRef<HTMLParagraphElement>(null);
  const load = async () => setInvites((await friendsApi.invites()).invites.filter((row) => row.purpose === "ai"));
  useEffect(() => { void load().catch(() => setError("Invitation status unavailable. Refresh before sharing."));
    const timer = window.setInterval(() => void load().catch(() => setError("Invitation status unavailable. Refresh before sharing.")), 5000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => { if (error) alert.current?.focus(); }, [error]);
  const act = async (fn: () => Promise<void>) => { setBusy(true); setError(""); setNotice("");
    try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : "Invitation could not be confirmed."); }
    finally { setBusy(false); }
  };
  const current = invites.find((row) => row.invite_id === created?.invite.invite_id);
  const usable = created && current?.status === "active" && Date.parse(current.expires_at) > Date.now();
  return <section aria-label="Invite someone to this AI">
    <h3>Invite a new friend to this AI</h3>
    <p>A single person can accept within 15 minutes. The first person who accepts gets permission for this service. Send privately; do not post the link publicly.</p>
    {error ? <p role="alert" tabIndex={-1} ref={alert}>{error}</p> : null}
    {notice ? <p role="status">{notice}</p> : null}
    <Button disabled={busy || disabled} onClick={() => confirm({ title: "Invite a friend to use this AI?", risk: "medium", confirmLabel: "Create AI invitation",
      body: "The first person who accepts will become your friend and receive permission for this AI service. Their questions are processed on your device. You can cancel the invitation or revoke access.",
      onConfirm: () => act(async () => { const result = await friendsApi.createAIInvite(serviceId); setCreated(result); await load();
        try { setQr(await QRCode.toDataURL(installationLink(result), { width: 256, margin: 4 })); } catch { setQr(""); }
        setNotice(""); }) })}>Invite someone to this AI</Button>
    <Button disabled={busy} onClick={() => void act(load)}>Refresh AI invitations</Button>
    {usable ? <div>
      <p role="status">Waiting for your friend to accept. No permission has been granted by this invitation yet.</p>
      {qr ? <img src={qr} width={256} height={256} alt="AI invitation QR code" /> : null}
      <textarea aria-label="AI invitation and instructions" readOnly value={invitationText(created)} rows={6} style={{ width: "100%" }} />
      <a href={installationLink(created)} target="_blank" rel="noreferrer">Open installation and invitation page</a>
      <Button disabled={busy} onClick={() => void act(async () => { await navigator.clipboard.writeText(invitationText(created)); setNotice("AI invitation copied."); })}>Copy AI invitation</Button>
    </div> : null}
    {invites.map((row) => <div key={row.invite_id}><p>{row.ai?.service.model_alias} · {row.status === "active" ? "Waiting for acceptance" : row.status === "used" ? row.ai_stage === "authorized" ? "AI authorization confirmed" : row.ai_stage === "revoked" ? "AI access revoked" : "Friendship recorded; AI authorization unfinished" : row.status} · expires {new Date(row.expires_at).toLocaleString()}</p>
      {row.status === "active" || row.status === "used" ? <Button disabled={busy} onClick={() => void act(async () => { await friendsApi.cancel(row.invite_id); await load(); setNotice("Invitation cancelled. New requests through its authorization are blocked."); })}>Cancel AI invitation</Button> : null}
    </div>)}
  </section>;
}
