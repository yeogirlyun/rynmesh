import { useState } from "react";
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
  const { space, spaceAction, demo, refresh, devices } = usePersonal();
  const { node, client } = useAppContext();
  const [name, setName] = useState("My space");
  const [deviceName, setDeviceName] = useState(node.node_name);
  const [invitation, setInvitation] = useState("");
  const [hours, setHours] = useState(24);
  const [busy, setBusy] = useState(false);
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
    setBusy(true);
    setError("");
    setCopied(false);
    try {
      const status = await spaceAction(action, body);
      if (action === "join") setInvitation("");
      return status;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not update your space.");
    } finally {
      setBusy(false);
    }
  }
  if (!space)
    return (
      <Note>
        Personal space is unavailable. Start the updated node, then{" "}
        <button className="pf-link" onClick={() => void refresh()}>
          try again
        </button>
        .
      </Note>
    );
  const active = space.membership === "active";
  return (
    <div className="pf-space-content">
      {demo && (
        <Note>
          Design preview. Invitations, permissions and recovery require a live
          node.
        </Note>
      )}
      {(error || space.last_error) && (
        <p className="pf-error" role="alert">
          {error || space.last_error}
        </p>
      )}
      {pending && (
        <div className="pf-space-notice" role="status">
          <RefreshCw size={18} />
          <div>
            <strong>Waiting for the coordinator</strong>
            <p>
              Keep Ryn running on the computer that created your space. Your
              request retries automatically.
            </p>
          </div>
        </div>
      )}
      {!space.space ? (
        <>
          <section className="pf-panel pf-space-section">
            <div className="pf-space-heading">
              <Users size={22} />
              <div>
                <h2>Create your personal space</h2>
                <p>Bring your computers together. No account required.</p>
              </div>
            </div>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void run("create", { name });
              }}
            >
              <label className="pf-field">
                <span>Space name</span>
                <input
                  value={name}
                  maxLength={32}
                  required
                  onChange={(e) => setName(e.target.value)}
                />
              </label>
              <Note>
                This computer becomes the coordinator. It approves invitations
                automatically when online. AI access starts with this device
                only.
              </Note>
              <button className="pf-button primary" disabled={disabled}>
                <Plus size={16} />
                Create space
              </button>
            </form>
          </section>
          <section className="pf-panel pf-space-section">
            <div className="pf-space-heading">
              <KeyRound size={22} />
              <div>
                <h2>Join an existing space</h2>
                <p>
                  Paste the invitation you brought from your other computer.
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
                <span>Device name</span>
                <input
                  value={deviceName}
                  maxLength={32}
                  required
                  onChange={(e) => setDeviceName(e.target.value)}
                />
              </label>
              <label className="pf-field">
                <span>Invitation</span>
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
              <Note>
                An invitation preauthorizes one device. Check who sent it before
                joining. The coordinator must be online; no second confirmation
                is needed there.
              </Note>
              <button
                className="pf-button primary"
                disabled={disabled || !invitation.trim()}
              >
                Join space
              </button>
            </form>
          </section>
          {pending && (
            <button
              className="pf-button"
              disabled={busy || demo}
              onClick={() =>
                setConfirmation({
                  title: "Cancel this request?",
                  detail:
                    "A join already accepted by the coordinator may still appear in its device list. A manager can remove that device.",
                  action: "leave",
                  body: {},
                })
              }
            >
              Cancel waiting
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
                  ? "Coordinator · Management device"
                  : space.can_manage
                    ? "Management device"
                    : "Member device"}{" "}
                ·{" "}
                {space.membership === "active"
                  ? "Membership verified"
                  : space.membership === "removed"
                    ? "Device removed"
                    : "Refresh required"}
              </p>
            </div>
          </div>
          {!active && (
            <Note>
              {space.membership === "removed"
                ? "This device was removed. Space AI access is blocked. Leave this space before joining again."
                : "Membership verification has expired. Start the coordinator and refresh to restore access. Device identities are preserved."}
            </Note>
          )}
          {space.can_manage ? (
            <section className="pf-panel pf-space-section">
              <h2>Add another computer</h2>
              <p>Create a single-use invitation before you leave home.</p>
              <label className="pf-field">
                <span>Invitation expires after</span>
                <select
                  value={hours}
                  onChange={(e) => setHours(Number(e.target.value))}
                >
                  <option value={1}>1 hour</option>
                  <option value={24}>24 hours</option>
                  <option value={72}>3 days</option>
                </select>
              </label>
              <button
                className="pf-button primary"
                disabled={disabled}
                onClick={() => void run("invite", { hours })}
              >
                <Plus size={16} />
                Create invitation
              </button>
              {space.invitation && (
                <div className="pf-space-invitation">
                  <label className="pf-field">
                    <span>One-device invitation</span>
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
                          "Could not copy. Select the invitation and copy it manually.",
                        );
                      }
                    }}
                  >
                    {copied ? <Check size={16} /> : <Copy size={16} />}
                    {copied ? "Copied" : "Copy invitation"}
                  </button>
                </div>
              )}
              <Note>
                Anyone holding this invitation can add one ordinary device
                before it expires. Keep it private. The coordinator must be
                online when it is redeemed.
              </Note>
            </section>
          ) : (
            active && (
              <Note>
                A management device can create an invitation for your next
                computer.
              </Note>
            )
          )}
          {!compact && (
            <>
              <section className="pf-panel pf-space-section">
                <h2>Devices in this space</h2>
                <p>
                  Devices keep their identity when their IP address changes.
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
                            ? " · This device"
                            : ""}
                        </strong>
                        <small>
                          {member.removed
                            ? "Removed"
                            : member.peer_id === space.space?.authority
                              ? "Coordinator"
                              : member.role === "manager"
                                ? "Management device"
                                : "Member device"}
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
                                      ? "Remove management permission?"
                                      : "Allow device management?",
                                  detail:
                                    member.role === "manager"
                                      ? `${member.name} will still be able to use shared services, but cannot add or remove devices.`
                                      : `${member.name} will be able to invite devices, change management permissions and remove other devices. Only enable this on computers you control.`,
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
                                ? "Make member"
                                : "Make manager"}
                            </button>
                            <button
                              className="pf-button"
                              disabled={disabled}
                              onClick={() =>
                                setConfirmation({
                                  title: `Remove ${member.name}?`,
                                  detail:
                                    "Its space access will be revoked as providers refresh membership. Offline providers may retain permission for up to 24 hours. This does not delete files or revoke separately issued API keys.",
                                  action: "remove",
                                  body: { peer_id: member.peer_id },
                                })
                              }
                            >
                              Remove
                            </button>
                          </div>
                        )}
                    </div>
                  ))}
                </div>
                <Note>
                  Membership changes are coordinated by the computer that
                  created the space. Other management devices can request
                  changes while it is online.
                </Note>
              </section>
              <SpaceAccess
                status={space}
                disabled={disabled || !active}
                onChange={(access) => void run("policy", { access })}
              />
              {space.coordinator && (
                <section className="pf-panel pf-space-section">
                  <h2>Invitations</h2>
                  {!space.invites.length && <p>No invitations yet.</p>}
                  {space.invites.map((invite) => (
                    <div className="pf-setting-row" key={invite.id}>
                      <div>
                        <h3>
                          {invite.revoked
                            ? "Cancelled"
                            : invite.used
                              ? "Used"
                              : invite.expires_at * 1000 < Date.now()
                                ? "Expired"
                                : "Available"}
                        </h3>
                        <p>
                          Expires{" "}
                          {new Date(invite.expires_at * 1000).toLocaleString()}
                        </p>
                      </div>
                      <button
                        className="pf-button"
                        disabled={disabled || invite.used || invite.revoked}
                        onClick={() =>
                          void run("cancel_invite", { id: invite.id })
                        }
                      >
                        Cancel invitation
                      </button>
                    </div>
                  ))}
                </section>
              )}
              {space.coordinator && (
                <section className="pf-panel pf-space-section">
                  <h2>Recovery backup</h2>
                  <p>
                    Save an encrypted copy of the coordinator identity and
                    current members.
                  </p>
                  <label className="pf-field">
                    <span>Recovery password</span>
                    <input
                      type="password"
                      autoComplete="new-password"
                      minLength={12}
                      maxLength={256}
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      placeholder="At least 12 characters"
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
                            : "Could not export recovery.",
                        );
                      } finally {
                        setBusy(false);
                      }
                    }}
                  >
                    <Download size={16} />
                    Download encrypted backup
                  </button>
                  <Note>
                    Keep the file and password safe. Restore using the
                    documented offline recovery tool into a fresh node folder,
                    with the old coordinator stopped. It restores the saved
                    member list; review permissions before sharing again.
                  </Note>
                </section>
              )}
              {!space.coordinator && (
                <button
                  className="pf-button"
                  disabled={busy || demo}
                  onClick={() =>
                    setConfirmation({
                      title: "Leave this space?",
                      detail:
                        "This computer will forget its space membership and stop serving space AI requests. A manager should also remove it from the space. Your local files are kept.",
                      action: "leave",
                      body: {},
                    })
                  }
                >
                  Leave space
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
          Refresh
        </button>
        {compact && (
          <Link className="pf-link" to={personalHref("/settings/space", demo)}>
            Manage personal space →
          </Link>
        )}
      </div>
      {confirmation && (
        <Modal title={confirmation.title} onClose={() => setConfirmation(null)}>
          <p>{confirmation.detail}</p>
          <footer className="pf-dialog-actions">
            <button className="pf-button" onClick={() => setConfirmation(null)}>
              Cancel
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
              Confirm
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
  return (
    <section className="pf-panel pf-space-section">
      <h2>AI access on this computer</h2>
      <p>
        Choose who can send AI tasks to this node. The model must also be
        configured and running.
      </p>
      <label className="pf-field">
        <span>Allow access from</span>
        <select
          aria-label="AI access"
          value={status.ai_access}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="local">This device only</option>
          <option value="space">My space devices</option>
        </select>
      </label>
      <Note>
        Only AI tasks are covered. Files and remote desktop are not enabled. API
        keys are separate credentials managed under API access.
      </Note>
    </section>
  );
}

export function PersonalSpacePage() {
  return (
    <div className="pf-page pf-space-page">
      <PageHeading
        title="Personal space"
        description="Your computers, connected by identity."
      />
      <SpaceControls />
    </div>
  );
}
