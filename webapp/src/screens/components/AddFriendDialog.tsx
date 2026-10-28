import { Globe, RefreshCw, ShieldCheck, TriangleAlert, UserPlus, Copy } from "lucide-react";
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
}

const when = formatWhen;

export default function AddFriendDialog(props: AddFriendProps) {
  useUILanguage();
  const { tab, onTab, onClose, busy, error, notice, reach, reachError, invite, qr, review } = props;
  const address = invite ? invite.invite : reach;
  return <Modal title={tr("Add a friend")} onClose={onClose} className="rf rf-dlg rf-dlg-hue rf-h-sky rq-dlg">
    <span className="rf-mark rf-tile rf-h-sky" aria-hidden="true"><UserPlus size={21} strokeWidth={2} /></span>
    <p className="rf-sub">{tr("Invite someone you know, or use an invitation they sent you.")}</p>
    <div className="rf-body">
      <div className="rf-seg rf-seg-fill" role="group" aria-label={tr("Invitation")}>
        <button type="button" aria-pressed={tab === "create"} onClick={() => onTab("create")}>{tr("Invite a friend")}</button>
        <button type="button" aria-pressed={tab === "use"} onClick={() => onTab("use")}>{tr("Use an invite")}</button>
      </div>
      {error ? <p className="rf-err" role="alert">{error}</p> : null}
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
          <textarea id="rq-paste" rows={3} spellCheck={false} placeholder={tr("Paste the invitation here")} value={props.paste} onChange={(event) => props.onPaste(event.target.value)} />
        </div>
        {!review ? <button type="button" className="rf-btn rf-btn-p rf-btn-lg rq-self-start" disabled={busy || !props.paste.trim()} onClick={props.onReview}>{tr("Review invite")}</button> : <div className="rq-review">
          <p className="rq-review-ok rf-t-ok"><ShieldCheck size={16} strokeWidth={2} aria-hidden="true" /><strong>{tr("Signature checked locally. Your friend has not been contacted.")}</strong></p>
          <dl>
            <div><dt>{tr("Device")}</dt><dd>{review.preview.node_name}</dd></div>
            <div><dt>{tr("Identity")}</dt><dd className="rf-mono">{review.preview.peer_id}</dd></div>
            <div><dt>{tr("Address")}</dt><dd className="rf-mono">{review.preview.endpoint} · {review.preview.address_category}</dd></div>
            <div><dt>{tr("Expires")}</dt><dd>{when(review.preview.expires_at)}</dd></div>
            <div><dt>{tr("Allows")}</dt><dd>{tr("Messages, attachments up to 5 MiB, content cards")}</dd></div>
            <div><dt>{tr("Stays off")}</dt><dd>{tr("AI, VPN, access to your files and credits")}</dd></div>
          </dl>
          <p className="rq-help">{tr(invitationReach(review.preview.address_category))} {tr("Ryn does not open router ports for this invite. Reachability has not been tested.")}</p>
        </div>}
      </div>
    </div>
    <footer className="rf-foot">
      <small className="rf-foot-note">{tab === "create" ? tr("Your friend pastes this on their device.") : tr("Nothing is sent until you add this friend.")}</small>
      <button type="button" className="rf-btn" onClick={onClose}>{tab === "create" ? tr("Done") : tr("Cancel")}</button>
      {tab === "use" && review ? <button type="button" className="rf-btn rf-btn-p" disabled={busy} onClick={props.onJoin}>{tr("Add this friend")}</button> : null}
    </footer>
  </Modal>;
}
