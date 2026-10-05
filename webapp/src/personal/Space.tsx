import { tr, uiLocale } from "../uiI18n";
import { useTranslation } from "react-i18next";
import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  Check,
  Copy,
  Download,
  KeyRound,
  Plus,
  RefreshCw,
  ShieldCheck,
  Users,
} from "lucide-react";
import { useAppContext } from "../appContext";
import { DeviceArt, Modal, Note, PageHeading } from "./components";
import { personalHref, usePersonal } from "./model";
import type { SpaceStatus } from "../domain/space";

export function SpaceControls({ compact = false }: { compact?: boolean }) {
  const { t } = useTranslation();
  const { space, spaceAction, demo, refresh, devices } = usePersonal();
  const { node, client } = useAppContext();
  const [name, setName] = useState("");
  const deviceName = node.node_name.trim().slice(0, 32) || t("personal.myDevice");
  const [invitation, setInvitation] = useState("");
  const [hours, setHours] = useState(24);
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState<{
    title: string;
    detail: string;
    action: string;
    body: Record<string, unknown>;
  } | null>(null);
  const pending = Boolean(space?.pending.length);
  const disabled = busy || pending || demo;
  async function run(action: string, body: Record<string, unknown> = {}) {
    if (submitting.current || demo || (pending && (action === "create" || action === "join"))) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    setCopied(false);
    try {
      const status = await spaceAction(action, body);
      if (action === "join") setInvitation("");
      return status;
    } catch (e) {
      setError(e instanceof Error ? e.message : t("personal.couldNotUpdateYourSpace"));
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  if (!space)
    return (
      <Note>
        {t("personal.personalSpaceIsUnavailableStartTheUpdatedNodeThen")}{" "}
        <button className="pf-link" onClick={() => void refresh()}>
          {t("personal.tryAgain")}
        </button>
        .
      </Note>
    );
  const active = space.membership === "active";
  return (
    <div className="pf-space-content">
      {demo && (
        <Note>
          {t("personal.designPreviewInvitationsPermissionsAndRecoveryRequireALiveNode")}
        </Note>
      )}
      {(error || space.last_error) && (
        <p className="pf-error" role="alert">
          {error || tr(space.last_error)}
        </p>
      )}
      {pending && (
        <div className="pf-space-notice" role="status">
          <RefreshCw size={18} />
          <div>
            <strong>{t("personal.waitingForTheCoordinator")}</strong>
            <p>
              {t("personal.keepRynRunningOnTheComputerThatCreatedYourSpaceYourRequestRetriesAutomatically")}
            </p>
          </div>
        </div>
      )}
      {!space.space ? (
        <>
          {!pending && <>
          <section className="pf-panel pf-space-section">
            <div className="pf-space-heading">
              <Users size={22} />
              <div>
                <h2>{t("personal.createYourPersonalSpace")}</h2>
                <p>{t("personal.bringYourComputersTogetherNoAccountRequired")}</p>
              </div>
            </div>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void run("create", { name: name.trim() || t("personal.mySpace") });
              }}
            >
              <label className="pf-field">
                <span>{t("personal.spaceName")}</span>
                <input
                  value={name}
                  placeholder={t("personal.mySpace")}
                  maxLength={32}
                  onChange={(e) => setName(e.target.value)}
                />
              </label>
              <Note>
                {t("personal.thisComputerBecomesTheCoordinatorItApprovesInvitationsAutomaticallyWhenOnlineAIAccessStartsWithThisDeviceOnly")}
              </Note>
              <button className="pf-button primary" disabled={disabled}>
                <Plus size={16} />
                {t("personal.createSpace")}
              </button>
            </form>
          </section>
          <section className="pf-panel pf-space-section">
            <div className="pf-space-heading">
              <KeyRound size={22} />
              <div>
                <h2>{t("personal.joinAnExistingSpace")}</h2>
                <p>
                  {t("personal.pasteTheInvitationYouBroughtFromYourOtherComputer")}
                </p>
              </div>
            </div>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void run("join", {
                  invitation: invitation.trim(),
                  name: deviceName,
                });
              }}
            >
              <label className="pf-field">
                <span>{t("personal.invitation")}</span>
                <textarea
                  rows={3}
                  autoComplete="off"
                  spellCheck={false}
                  value={invitation}
                  required
                  maxLength={32000}
                  placeholder="ryn-invite-v1:…"
                  onChange={(e) => setInvitation(e.target.value)}
                />
              </label>
              <small>{t("personal.joinUsingThisDeviceName", { name: deviceName })}</small>
              <Note>
                {t("personal.anInvitationPreauthorizesOneDeviceCheckWhoSentItBeforeJoiningTheCoordinatorMustBeOnlineNoSecondConfirmationIsNeededThere")}
              </Note>
              <button
                className="pf-button primary"
                disabled={disabled || !invitation.trim()}
              >
                {t("personal.joinSpace")}
              </button>
            </form>
          </section>
          </>}
          {pending && (
            <button
              className="pf-button"
              disabled={busy || demo}
              onClick={() =>
                setConfirmation({
                  title: t("personal.cancelThisRequest"),
                  detail:
                    t("personal.aJoinAlreadyAcceptedByTheCoordinatorMayStillAppearInItsDeviceListAManagerCanRemoveThatDevice"),
                  action: "leave",
                  body: {},
                })
              }
            >
              {t("personal.cancelWaiting")}
            </button>
          )}
        </>
      ) : (
        <>
          <div className="pf-space-summary">
            <span className="pf-space-emblem">
              <ShieldCheck size={28} />
            </span>
            <div>
              <h2>{space.space.name}</h2>
              <p>
                {space.coordinator
                  ? t("personal.coordinatorManagementDevice")
                  : space.can_manage
                    ? t("personal.managementDevice")
                    : t("personal.memberDevice")}{" "}
                ·{" "}
                {space.membership === "active"
                  ? t("personal.membershipVerified")
                  : space.membership === "removed"
                    ? t("personal.deviceRemoved")
                    : t("personal.refreshRequired")}
              </p>
            </div>
          </div>
          {!active && (
            <Note>
              {space.membership === "removed"
                ? t("personal.thisDeviceWasRemovedSpaceAIAccessIsBlockedLeaveThisSpaceBeforeJoiningAgain")
                : t("personal.membershipVerificationHasExpiredStartTheCoordinatorAndRefreshToRestoreAccessDeviceIdentitiesArePreserved")}
            </Note>
          )}
          {space.can_manage ? (
            <section className="pf-panel pf-space-section">
              <h2>{t("personal.addAnotherComputer")}</h2>
              <p>{t("personal.createASingleuseInvitationBeforeYouLeaveHome")}</p>
              <label className="pf-field">
                <span>{t("personal.invitationExpiresAfter")}</span>
                <select
                  value={hours}
                  onChange={(e) => setHours(Number(e.target.value))}
                >
                  <option value={1}>{t("personal.1Hour")}</option>
                  <option value={24}>{t("personal.24Hours")}</option>
                  <option value={72}>{t("personal.3Days")}</option>
                </select>
              </label>
              <button
                className="pf-button primary"
                disabled={disabled}
                onClick={() => void run("invite", { hours })}
              >
                <Plus size={16} />
                {t("personal.createInvitation")}
              </button>
              {space.invitation && (
                <div className="pf-space-invitation">
                  <label className="pf-field">
                    <span>{t("personal.onedeviceInvitation")}</span>
                    <textarea
                      rows={3}
                      readOnly
                      value={space.invitation}
                      spellCheck={false}
                    />
                  </label>
                  <button
                    className="pf-button"
                    onClick={async () => {
                      try {
                        await navigator.clipboard.writeText(space.invitation);
                        setCopied(true);
                      } catch {
                        setError(
                          t("personal.couldNotCopySelectTheInvitationAndCopyItManually"),
                        );
                      }
                    }}
                  >
                    {copied ? <Check size={16} /> : <Copy size={16} />}
                    {copied ? t("personal.copied") : t("personal.copyInvitation")}
                  </button>
                </div>
              )}
              <Note>
                {t("personal.anyoneHoldingThisInvitationCanAddOneOrdinaryDeviceBeforeItExpiresKeepItPrivateTheCoordinatorMustBeOnlineWhenItIsRedeemed")}
              </Note>
            </section>
          ) : (
            active && (
              <Note>
                {t("personal.aManagementDeviceCanCreateAnInvitationForYourNextComputer")}
              </Note>
            )
          )}
          {!compact && (
            <>
              <section className="pf-panel pf-space-section">
                <h2>{t("personal.devicesInThisSpace")}</h2>
                <p>
                  {t("personal.devicesKeepTheirIdentityWhenTheirIPAddressChanges")}
                </p>
                <div className="pf-space-members">
                  {space.members.map((member) => (
                    <div className="pf-space-member" key={member.peer_id}>
                      <DeviceArt
                        small
                        kind={
                          devices.find((device) => device.id === member.peer_id)
                            ?.kind
                        }
                      />
                      <div className="pf-space-member-name">
                        <strong>
                          {member.name}
                          {member.peer_id === space.self_id
                            ? t("personal.thisDeviceSuffix")
                            : ""}
                        </strong>
                        <small>
                          {member.removed
                            ? t("personal.removed")
                            : member.peer_id === space.space?.authority
                              ? t("personal.coordinator")
                              : member.role === "manager"
                                ? t("personal.managementDevice")
                                : t("personal.memberDevice")}
                        </small>
                        <code title={member.peer_id}>
                          {member.peer_id.slice(0, 12)}…
                        </code>
                      </div>
                      {space.can_manage &&
                        member.peer_id !== space.space?.authority &&
                        !member.removed && (
                          <div className="pf-space-member-actions">
                            <button
                              className="pf-button"
                              disabled={disabled}
                              onClick={() =>
                                setConfirmation({
                                  title:
                                    member.role === "manager"
                                      ? t("personal.removeManagementPermission")
                                      : t("personal.allowDeviceManagement"),
                                  detail:
                                    member.role === "manager"
                                      ? t("personal.nameWillStillBeAbleToUseSharedServicesButCannotAddOrRemoveDevices", { name: member.name })
                                      : t("personal.nameWillBeAbleToInviteDevicesChangeManagementPermissionsAndRemoveOtherDevicesOnlyEnableThisOnComputersYouControl", { name: member.name }),
                                  action: "role",
                                  body: {
                                    peer_id: member.peer_id,
                                    role:
                                      member.role === "manager"
                                        ? "device"
                                        : "manager",
                                  },
                                })
                              }
                            >
                              {member.role === "manager"
                                ? t("personal.makeMember")
                                : t("personal.makeManager")}
                            </button>
                            <button
                              className="pf-button"
                              disabled={disabled}
                              onClick={() =>
                                setConfirmation({
                                  title: t("personal.removeName", { name: member.name }),
                                  detail:
                                    t("personal.itsSpaceAccessWillBeRevokedAsProvidersRefreshMembershipOfflineProvidersMayRetainPermissionForUpTo24HoursThisDoesNotDeleteFilesOrRevokeSeparatelyIssuedAPIKeys"),
                                  action: "remove",
                                  body: { peer_id: member.peer_id },
                                })
                              }
                            >
                              {t("personal.remove")}
                            </button>
                          </div>
                        )}
                    </div>
                  ))}
                </div>
                <Note>
                  {t("personal.membershipChangesAreCoordinatedByTheComputerThatCreatedTheSpaceOtherManagementDevicesCanRequestChangesWhileItIsOnline")}
                </Note>
              </section>
              <SpaceAccess
                status={space}
                disabled={disabled || !active}
                onChange={(access) => void run("policy", { access })}
              />
              {space.coordinator && (
                <section className="pf-panel pf-space-section">
                  <h2>{t("personal.invitations")}</h2>
                  {!space.invites.length && <p>{t("personal.noInvitationsYet")}</p>}
                  {space.invites.map((invite) => (
                    <div className="pf-setting-row" key={invite.id}>
                      <div>
                        <h3>
                          {invite.revoked
                            ? t("personal.cancelled")
                            : invite.used
                              ? t("personal.used")
                              : invite.expires_at * 1000 < Date.now()
                                ? t("personal.expired")
                                : t("personal.available")}
                        </h3>
                        <p>
                          {t("personal.expires")}{" "}
                          {new Date(invite.expires_at * 1000).toLocaleString(uiLocale())}
                        </p>
                      </div>
                      <button
                        className="pf-button"
                        disabled={disabled || invite.used || invite.revoked}
                        onClick={() =>
                          void run("cancel_invite", { id: invite.id })
                        }
                      >
                        {t("personal.cancelInvitation")}
                      </button>
                    </div>
                  ))}
                </section>
              )}
              {space.coordinator && (
                <section className="pf-panel pf-space-section">
                  <h2>{t("personal.recoveryBackup")}</h2>
                  <p>
                    {t("personal.saveAnEncryptedCopyOfTheCoordinatorIdentityAndCurrentMembers")}
                  </p>
                  <label className="pf-field">
                    <span>{t("personal.recoveryPassword")}</span>
                    <input
                      type="password"
                      autoComplete="new-password"
                      minLength={12}
                      maxLength={256}
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      placeholder={t("personal.atLeast12Characters")}
                    />
                  </label>
                  <button
                    className="pf-button"
                    disabled={disabled || password.length < 12}
                    onClick={async () => {
                      setBusy(true);
                      setError("");
                      try {
                        const backup = await client.spaceBackup(password);
                        const url = URL.createObjectURL(
                          new Blob([JSON.stringify(backup, null, 2)], {
                            type: "application/json",
                          }),
                        );
                        const a = document.createElement("a");
                        a.href = url;
                        a.download = "ryn-space-recovery.json";
                        a.click();
                        setTimeout(() => URL.revokeObjectURL(url), 1000);
                        setPassword("");
                      } catch (e) {
                        setError(
                          e instanceof Error
                            ? e.message
                            : t("personal.couldNotExportRecovery"),
                        );
                      } finally {
                        setBusy(false);
                      }
                    }}
                  >
                    <Download size={16} />
                    {t("personal.downloadEncryptedBackup")}
                  </button>
                  <Note>
                    {t("personal.keepTheFileAndPasswordSafeRestoreUsingTheDocumentedOfflineRecoveryToolIntoAFreshNodeFolderWithTheOldCoordinatorStoppedItRestoresTheSavedMemberListReviewPermissionsBeforeSharingAgain")}
                  </Note>
                </section>
              )}
              {!space.coordinator && (
                <button
                  className="pf-button"
                  disabled={busy || demo}
                  onClick={() =>
                    setConfirmation({
                      title: t("personal.leaveThisSpace"),
                      detail:
                        t("personal.thisComputerWillForgetItsSpaceMembershipAndStopServingSpaceAIRequestsAManagerShouldAlsoRemoveItFromTheSpaceYourLocalFilesAreKept"),
                      action: "leave",
                      body: {},
                    })
                  }
                >
                  {t("personal.leaveSpace")}
                </button>
              )}
            </>
          )}
        </>
      )}
      <div className="pf-dialog-actions">
        <button
          className="pf-button"
          disabled={busy || demo}
          onClick={() => void run("sync")}
        >
          <RefreshCw size={16} />
          {t("personal.refresh")}
        </button>
        {compact && (
          <Link className="pf-link" to={personalHref("/settings/space", demo)}>
            {t("personal.managePersonalSpace")}
          </Link>
        )}
      </div>
      {confirmation && (
        <Modal title={confirmation.title} onClose={() => setConfirmation(null)}>
          <p>{confirmation.detail}</p>
          <footer className="pf-dialog-actions">
            <button className="pf-button" onClick={() => setConfirmation(null)}>
              {t("personal.cancel")}
            </button>
            <button
              className="pf-button primary"
              disabled={busy}
              onClick={async () => {
                const result = await run(
                  confirmation.action,
                  confirmation.body,
                );
                if (result) setConfirmation(null);
              }}
            >
              {t("personal.confirm")}
            </button>
          </footer>
        </Modal>
      )}
    </div>
  );
}

function SpaceAccess({
  status,
  disabled,
  onChange,
}: {
  status: SpaceStatus;
  disabled: boolean;
  onChange: (access: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <section className="pf-panel pf-space-section">
      <h2>{t("personal.aiAccessOnThisComputer")}</h2>
      <p>
        {t("personal.chooseWhoCanSendAITasksToThisNodeTheModelMustAlsoBeConfiguredAndRunning")}
      </p>
      <label className="pf-field">
        <span>{t("personal.allowAccessFrom")}</span>
        <select
          aria-label={t("personal.aiAccess")}
          value={status.ai_access}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="local">{t("personal.thisDeviceOnly")}</option>
          <option value="space">{t("personal.mySpaceDevices")}</option>
        </select>
      </label>
      <Note>
        {t("personal.onlyAITasksAreCoveredFilesAndRemoteDesktopAreNotEnabledAPIKeysAreSeparateCredentialsManagedUnderAPIAccess")}
      </Note>
    </section>
  );
}

export function PersonalSpacePage() {
  const { t } = useTranslation();
  return (
    <div className="pf-page pf-space-page">
      <PageHeading
        title={t("personal.personalSpace")}
        description={t("personal.yourComputersConnectedByIdentity")}
      />
      <SpaceControls />
    </div>
  );
}
