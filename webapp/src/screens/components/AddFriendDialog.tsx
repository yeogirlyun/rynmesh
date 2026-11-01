import { Globe, RefreshCw, ShieldCheck, TriangleAlert, UserPlus, Copy } from "lucide-react";
import { useEffect, useRef } from "react";
import type { FriendInvitePreview, FriendInviteResult } from "../../domain/friendTypes";
import { invitationReach, invitationText } from "../../domain/friendsClient";
import { Modal } from "../../personal/components";
import { tr, useUILanguage } from "../../uiI18n";
import { formatWhen } from "./FriendsParts";
import "../friends-refresh.css";

export type InviteTab = "create" | "use";

/** Everything the Add a friend dialog shows. The invite state itself stays in the Friends page, so closing the dialog keeps an open invite. */
export interface AddFriendProps {
  tab: InviteTab;
  onTab: (tab: InviteTab) => void;
  onClose: () => void;
  busy: boolean;
  error: string;
  notice: string;
  reach: { endpoint: string; address_category: string } | null;
  reachError: string;
  onRefreshReach: () => void;
  invite: FriendInviteResult | null;
  qr: string;
  onCreate: () => void;
  onCopy: () => void;
  onCancelInvite: () => void;
  paste: string;
  onPaste: (value: string) => void;
  review: { uri: string; preview: FriendInvitePreview } | null;
  onReview: () => void;
  onJoin: () => void;
  reconnecting?: boolean;
  onCancelReview: () => void;
}

const when = formatWhen;

export default function AddFriendDialog(props: AddFriendProps) {
  useUILanguage();
  const { tab, onTab, onClose, busy, error, notice, reach, reachError, invite, qr, review } = props;
  const address = invite ? invite.invite : reach;
  const pasteRef = useRef<HTMLTextAreaElement>(null);
  const reviewRef = useRef<HTMLHeadingElement>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);
  useEffect(() => { if (tab === "use" && !review) pasteRef.current?.focus(); }, [tab, review]);
  useEffect(() => { if (review) reviewRef.current?.focus(); }, [review]);
  useEffect(() => { if (error) errorRef.current?.focus(); }, [error]);
  return <Modal title={tr("Add a friend")} onClose={onClose} className="rf rf-dlg rf-dlg-hue rf-h-sky rq-dlg">
    <span className="rf-mark rf-tile rf-h-sky" aria-hidden="true"><UserPlus size={21} strokeWidth={2} /></span>
    <p className="rf-sub">{tr("Invite someone you know, or use an invitation they sent you.")}</p>
    <div className="rf-body">
      <div className="rf-seg rf-seg-fill" role="group" aria-label={tr("Invitation")}>
        <button type="button" aria-pressed={tab === "create"} onClick={() => onTab("create")}>{tr("Invite a friend")}</button>
        <button type="button" aria-pressed={tab === "use"} onClick={() => onTab("use")}>{tr("Use an invite")}</button>
      </div>
      {error ? <p ref={errorRef} tabIndex={-1} className="rf-err" role="alert">{error}</p> : null}
      {notice ? <p className="rq-dlg-notice" role="status">{notice}</p> : null}

      <div className="rq-pane" data-on={tab === "create"}>
        <p className="rq-lead">{tr("One use, valid for 15 minutes. Allows messages, small attachments and content cards. AI access stays off.")}</p>
        <div className="rq-reach">
          <Globe size={17} strokeWidth={1.7} aria-hidden="true" />
          <div className="rq-reach-text">
            <small>{tr("Sharing address")}</small>
            {address ? <>
              <b className="rf-mono">{address.endpoint} <span>· {address.address_category}</span></b>
              <p>{tr(invitationReach(address.address_category))}</p>
            </> : <p role={reachError ? "alert" : "status"}>{reachError || tr("Loading sharing address…")}</p>}
          </div>
          {!invite ? <button type="button" className="rf-ib rf-ib-sm" aria-label={tr("Refresh sharing address")} title={tr("Refresh sharing address")} disabled={busy} onClick={props.onRefreshReach}><RefreshCw size={15} strokeWidth={1.7} aria-hidden="true" /></button> : null}
        </div>
        {!invite ? <button type="button" className="rf-btn rf-btn-p rf-btn-lg rq-self-start" disabled={busy || !reach} onClick={props.onCreate}>{tr("Create invite")}</button> : <div className="rq-invite">
          {qr ? <img className="rq-qr" src={qr} alt={tr("Friend invitation QR code")} /> : <span className="rq-qr rq-qr-wait" aria-hidden="true" />}
          <div className="rq-invite-text">
            <div className="rq-invite-meta"><span className="rf-pill rf-t-ok"><i aria-hidden="true" />{tr("Ready to share")}</span><small>{tr("Expires {{time}}", { time: when(invite.invite.expires_at) })}</small></div>
            <label htmlFor="rq-invite-text">{tr("Invitation and installation instructions")}</label>
            <textarea id="rq-invite-text" className="rf-inp rf-mono" rows={4} readOnly value={invitationText(invite)} />
            <div className="rq-actions">
              <button type="button" className="rf-btn rf-btn-p rf-btn-sm" disabled={busy} onClick={props.onCopy}><Copy size={13} strokeWidth={1.9} aria-hidden="true" />{tr("Copy invitation")}</button>
              <button type="button" className="rf-btn rf-btn-sm" disabled={busy} onClick={props.onCancelInvite}>{tr("Cancel invite")}</button>
            </div>
          </div>
        </div>}
        <p className="rq-warn-note"><TriangleAlert size={15} strokeWidth={1.8} aria-hidden="true" />{tr("Ryn does not open router ports for this invite. No connection has been tested.")}</p>
      </div>

      <div className="rq-pane" data-on={tab === "use"}>
        <div className="rq-paste">
          <label htmlFor="rq-paste">{tr("Friend invite")}</label>
          <textarea ref={pasteRef} id="rq-paste" rows={3} spellCheck={false} placeholder={tr("Paste the invitation here")} value={props.paste} onChange={(event) => props.onPaste(event.target.value)} />
        </div>
        {!review ? <button type="button" className="rf-btn rf-btn-p rf-btn-lg rq-self-start" disabled={busy || !props.paste.trim()} onClick={props.onReview}>{tr("Review invite")}</button> : <div className="rq-review">
          <h3 ref={reviewRef} tabIndex={-1}>{review.preview.purpose === "ai" ? "Review this friend's AI" : tr("Review invite")}</h3>
          <p className="rq-review-ok rf-t-ok"><ShieldCheck size={16} strokeWidth={2} aria-hidden="true" /><strong>{review.preview.purpose === "ai" ? "Provider signature checked. Current AI details confirmed with their device." : tr("Signature checked locally. Your friend has not been contacted.")}</strong></p>
          <dl>
            <div><dt>{tr("Device")}</dt><dd>{review.preview.node_name}</dd></div>
            <div><dt>{tr("Identity")}</dt><dd className="rf-mono">{review.preview.peer_id}</dd></div>
            <div><dt>{tr("Address")}</dt><dd className="rf-mono">{review.preview.endpoint} · {review.preview.address_category}</dd></div>
            <div><dt>{tr("Expires")}</dt><dd>{when(review.preview.expires_at)}</dd></div>
            <div><dt>{tr("Allows")}</dt><dd>{tr("Messages, attachments up to 5 MiB, content cards")}</dd></div>
            <div><dt>{tr("Stays off")}</dt><dd>{review.preview.purpose === "ai" ? "Other AI services, VPN, access to your files and credits" : tr("AI, VPN, access to your files and credits")}</dd></div>
          </dl>
          {review.preview.ai && <section aria-label={tr("AI invitation details")}>
            <p>{tr("Model:")} {review.preview.ai.service.model_alias} · {review.preview.ai.ready ? "Model ready" : "Model not ready"} · {review.preview.ai.sharing ? "Sharing enabled" : "Sharing paused"}</p>
            <p>{tr("Your questions go to")} {review.preview.node_name}{tr("'s device. No model download or API key is needed here.")}</p>
            <p>{review.preview.ai.service.pricing ? `Development Task Balance: input ${review.preview.ai.service.pricing.input_per_1k}/1k tokens, output ${review.preview.ai.service.pricing.output_per_1k}/1k, minimum ${review.preview.ai.service.pricing.minimum}, maximum per task ${review.preview.ai.service.pricing.maximum_per_task}. This is not RMB billing.` : "Price unknown. Do not send a question until the actual price is confirmed."}</p>
            {review.preview.changed && <p role="status">{tr("The model or price differs from the original invitation. Review these current details before accepting.")}</p>}
          </section>}
          {props.reconnecting && <p>{tr("Verify this device at its new address. Keep the original relationship, AI permissions and history. This does not restore revoked access.")}</p>}
          <p className="rq-help">{tr(invitationReach(review.preview.address_category))} {tr("Ryn does not open router ports for this invite. Reachability has not been tested.")}</p>
        </div>}
      </div>
    </div>
    <footer className="rf-foot">
      <small className="rf-foot-note">{tab === "create" ? tr("Your friend pastes this on their device.") : tr("Nothing is sent until you add this friend.")}</small>
      <button type="button" className="rf-btn" onClick={onClose}>{tab === "create" ? tr("Done") : tr("Cancel")}</button>
      {tab === "use" && review ? <button type="button" className="rf-btn" disabled={busy} onClick={props.onCancelReview}>{tr("Cancel review")}</button> : null}
      {tab === "use" && review ? <button type="button" className="rf-btn rf-btn-p" disabled={busy} onClick={props.onJoin}>{review.preview.purpose === "ai" ? "Accept and start chatting" : props.reconnecting ? "Update this friend's address" : tr("Add this friend")}</button> : null}
    </footer>
  </Modal>;
}
