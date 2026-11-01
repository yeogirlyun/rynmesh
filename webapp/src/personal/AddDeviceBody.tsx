import { useId } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Check, Copy, Info, KeyRound, Link2, Plus, RefreshCw, ShieldAlert, Users } from "lucide-react";
import { tr } from "../uiI18n";
import type { SpaceStatus } from "../domain/space";
import { personalHref } from "./model";
import { Pill, type Tone } from "./drop/FamilyParts";

/** The compact (Add device dialog) body of SpaceControls. State, actions and permissions stay in SpaceControls; this only draws them. */
export function AddDeviceBody({ space, demo, busy, disabled, pending, error, copied, name, setName, invitation, setInvitation, deviceName, hours, setHours, run, onCopy, onCancelPending }: {
  space: SpaceStatus; demo: boolean; busy: boolean; disabled: boolean; pending: boolean; error: string; copied: boolean;
  name: string; setName: (value: string) => void; invitation: string; setInvitation: (value: string) => void; deviceName: string;
  hours: number; setHours: (value: number) => void;
  run: (action: string, body?: Record<string, unknown>) => Promise<unknown>; onCopy: () => void; onCancelPending: () => void;
}) {
  const { t } = useTranslation();
  const expiryLabel = useId();
  const active = space.membership === "active";
  const tone: Tone = space.membership === "active" ? "ok" : space.membership === "removed" ? "bad" : "warn";
  const expiries = [[1, t("personal.1Hour")], [24, t("personal.24Hours")], [72, t("personal.3Days")]] as const;
  return (
    <div className="rf-body rf-add">
      {demo && <p className="rf-callout"><Info size={15} aria-hidden="true" /><span>{t("personal.designPreviewInvitationsPermissionsAndRecoveryRequireALiveNode")}</span></p>}
      {(error || space.last_error) && <p className="rf-err" role="alert">{error || tr(space.last_error)}</p>}
      {pending && <div className="rf-callout rf-callout-brand" role="status"><RefreshCw size={16} aria-hidden="true" />
        <div><b className="rf-h3">{t("personal.waitingForTheCoordinator")}</b>
          <p>{t("personal.keepRynRunningOnTheComputerThatCreatedYourSpaceYourRequestRetriesAutomatically")}</p></div></div>}
      {!space.space ? (
        <>
          {!pending && <>
            <form className="rf-box" onSubmit={(event) => { event.preventDefault(); void run("create", { name: name.trim() || t("personal.mySpace") }); }}>
              <b className="rf-h3"><Users size={15} aria-hidden="true" /> {t("personal.createYourPersonalSpace")}</b>
              <label className="rf-field"><span>{t("personal.spaceName")}</span>
                <input className="rf-inp rf-inp-s" value={name} placeholder={t("personal.mySpace")} maxLength={32} onChange={(event) => setName(event.target.value)} /></label>
              <small className="rf-hint">{t("personal.thisComputerBecomesTheCoordinatorItApprovesInvitationsAutomaticallyWhenOnlineAIAccessStartsWithThisDeviceOnly")}</small>
              <button className="rf-btn rf-btn-p" disabled={disabled}><Plus size={15} aria-hidden="true" />{t("personal.createSpace")}</button>
            </form>
            <form className="rf-box rf-box-plain" onSubmit={(event) => { event.preventDefault(); void run("join", { invitation: invitation.trim(), name: deviceName }); }}>
              <b className="rf-h3"><KeyRound size={15} aria-hidden="true" /> {t("personal.joinAnExistingSpace")}</b>
              <label className="rf-field"><span>{t("personal.invitation")}</span>
                <textarea className="rf-inp rf-inp-s rf-mono" rows={3} autoComplete="off" spellCheck={false} value={invitation} required maxLength={32000} placeholder="ryn-invite-v1:…"
                  onChange={(event) => setInvitation(event.target.value)} /></label>
              <small className="rf-hint">{t("personal.joinUsingThisDeviceName", { name: deviceName })}</small>
              <small className="rf-hint">{t("personal.anInvitationPreauthorizesOneDeviceCheckWhoSentItBeforeJoiningTheCoordinatorMustBeOnlineNoSecondConfirmationIsNeededThere")}</small>
              <button className="rf-btn" disabled={disabled || !invitation.trim()}>{t("personal.joinSpace")}</button>
            </form>
          </>}
          {pending && <button type="button" className="rf-btn" disabled={busy || demo} onClick={onCancelPending}>{t("personal.cancelWaiting")}</button>}
        </>
      ) : (
        <>
          <div className="rf-member rf-h-indigo">
            <span className="rf-chipicon"><Users size={18} strokeWidth={1.7} aria-hidden="true" /></span>
            <div className="rf-member-text"><b>{space.space.name}</b>
              <small>{space.coordinator ? t("personal.coordinatorManagementDevice") : space.can_manage ? t("personal.managementDevice") : t("personal.memberDevice")}</small></div>
            <Pill tone={tone}>{space.membership === "active" ? t("personal.membershipVerified") : space.membership === "removed" ? t("personal.deviceRemoved") : t("personal.refreshRequired")}</Pill>
          </div>
          {!active && <p className="rf-callout rf-callout-warn"><ShieldAlert size={15} aria-hidden="true" /><span>{space.membership === "removed"
            ? t("personal.thisDeviceWasRemovedSpaceAIAccessIsBlockedLeaveThisSpaceBeforeJoiningAgain")
            : t("personal.membershipVerificationHasExpiredStartTheCoordinatorAndRefreshToRestoreAccessDeviceIdentitiesArePreserved")}</span></p>}
          {space.can_manage ? (
            <>
              <section className="rf-sec" aria-labelledby={`${expiryLabel}-t`}>
                <div><h3 id={`${expiryLabel}-t`} className="rf-h3">{t("personal.addAnotherComputer")}</h3>
                  <p className="rf-lead">{t("personal.createASingleuseInvitationBeforeYouLeaveHome")}</p></div>
                <div className="rf-expiry">
                  <span id={expiryLabel} className="rf-label">{t("personal.invitationExpiresAfter")}</span>
                  <div className="rf-seg" role="group" aria-labelledby={expiryLabel}>
                    {expiries.map(([value, label]) => <button key={value} type="button" aria-pressed={hours === value} onClick={() => setHours(value)}>{label}</button>)}
                  </div>
                </div>
                <button type="button" className={`rf-btn rf-btn-lg rf-btn-block${space.invitation ? "" : " rf-btn-p"}`} disabled={disabled} onClick={() => void run("invite", { hours })}>
                  <Link2 size={16} aria-hidden="true" />{t("personal.createInvitation")}</button>
                {space.invitation && <div className="rf-box">
                  <label className="rf-label" htmlFor={`${expiryLabel}-inv`}>{t("personal.onedeviceInvitation")}</label>
                  <textarea id={`${expiryLabel}-inv`} className="rf-inp rf-invite-text rf-mono" rows={3} readOnly value={space.invitation} spellCheck={false} />
                  <button type="button" className="rf-btn rf-btn-p rf-btn-block" onClick={onCopy}>{copied ? <Check size={15} strokeWidth={2} aria-hidden="true" /> : <Copy size={15} aria-hidden="true" />}{copied ? t("personal.copied") : t("personal.copyInvitation")}</button>
                </div>}
                <p className="rf-callout rf-callout-warn" role="note"><ShieldAlert size={15} aria-hidden="true" /><span>{t("personal.anyoneHoldingThisInvitationCanAddOneOrdinaryDeviceBeforeItExpiresKeepItPrivateTheCoordinatorMustBeOnlineWhenItIsRedeemed")}</span></p>
              </section>
              <section className="rf-sec" aria-labelledby={`${expiryLabel}-s`}>
                <h3 id={`${expiryLabel}-s`} className="rf-eyebrow">{t("personal.onYourOtherComputer")}</h3>
                <ol className="rf-list rf-steps">
                  <li><b>1</b>{t("personal.addDeviceStep1")}</li>
                  <li><b>2</b>{t("personal.addDeviceStep2")}</li>
                  <li><b>3</b>{t("personal.addDeviceStep3")}</li>
                </ol>
              </section>
            </>
          ) : (active && <p className="rf-callout"><Info size={15} aria-hidden="true" /><span>{t("personal.aManagementDeviceCanCreateAnInvitationForYourNextComputer")}</span></p>)}
        </>
      )}
      <div className="rf-add-actions">
        <button type="button" className="rf-btn rf-btn-sm" disabled={busy || demo} onClick={() => void run("sync")}><RefreshCw size={14} aria-hidden="true" />{t("personal.refresh")}</button>
        <Link className="rf-lnk" to={personalHref("/settings/space", demo)}>{t("personal.managePersonalSpace")}</Link>
      </div>
    </div>
  );
}
