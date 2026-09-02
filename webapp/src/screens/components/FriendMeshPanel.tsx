import { Copy, QrCode, ShieldAlert, UserMinus, UserPlus } from "lucide-react";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import type {
  FriendInviteRecord,
  FriendInviteReview,
  FriendJoinResult,
  FriendPermission,
  FriendRecord,
  NodeClient,
} from "../../domain/nodeClient";
import { endpointAddressClass, splitEndpoints } from "../../domain/friendMesh";
import { friendInviteQrDataUrl } from "../../domain/friendMeshQr";
import type { ConfirmRequest, ToastMessage } from "../../domain/types";
import { Button, Chip, Hash, Panel } from "../../components/ui";

const permissionOptions: Array<{ value: FriendPermission; label: string }> = [
  { value: "private-ai.use", label: "Use my Private AI" },
  { value: "peer.messaging", label: "Send peer messages" },
  { value: "peer.discovery", label: "Peer discovery" },
];

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "The local node could not complete this request.";
}

function formatTime(value: string | null): string {
  if (!value) return "never";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function EndpointList({ endpoints }: { endpoints: string[] }) {
  return (
    <ul className="friend-endpoint-list">
      {endpoints.map((endpoint) => {
        const addressClass = endpointAddressClass(endpoint);
        const risky = addressClass === "private LAN" || addressClass === "unresolved hostname";
        return (
          <li key={endpoint}>
            <code>{endpoint}</code>
            <Chip tone={risky ? "warn" : addressClass.startsWith("blocked") || addressClass === "invalid" ? "danger" : "muted"}>
              {addressClass}
            </Chip>
          </li>
        );
      })}
    </ul>
  );
}

function inviteState(invite: FriendInviteRecord): { label: string; tone: "ok" | "warn" | "danger" | "muted" } {
  if (invite.used_at) return { label: "used", tone: "ok" };
  if (invite.cancelled_at) return { label: "cancelled", tone: "danger" };
  if (new Date(invite.expires_at).getTime() <= Date.now()) return { label: "expired", tone: "muted" };
  return { label: "active", tone: "warn" };
}

export default function FriendMeshPanel({
  client,
  confirm,
  notify,
}: {
  client: NodeClient;
  confirm: (request: ConfirmRequest) => void;
  notify: (tone: ToastMessage["tone"], text: string) => void;
}) {
  const [friends, setFriends] = useState<FriendRecord[]>([]);
  const [invites, setInvites] = useState<FriendInviteRecord[]>([]);
  const [endpointsText, setEndpointsText] = useState("");
  const [permissions, setPermissions] = useState<FriendPermission[]>(["private-ai.use"]);
  const [ttlSeconds, setTtlSeconds] = useState(900);
  const [endpointRiskAccepted, setEndpointRiskAccepted] = useState(false);
  const [created, setCreated] = useState<{ invite: FriendInviteRecord; link: string } | null>(null);
  const [reviewLink, setReviewLink] = useState("");
  const [allowLanReview, setAllowLanReview] = useState(false);
  const [review, setReview] = useState<FriendInviteReview | null>(null);
  const [pendingJoin, setPendingJoin] = useState<FriendJoinResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const createdHeading = useRef<HTMLHeadingElement>(null);
  const reviewHeading = useRef<HTMLHeadingElement>(null);

  const endpoints = useMemo(() => splitEndpoints(endpointsText), [endpointsText]);
  const hasInvalidEndpoint = endpoints.some((endpoint) => {
    const addressClass = endpointAddressClass(endpoint);
    return addressClass === "invalid" || addressClass === "blocked local/link-local";
  });

  const refresh = async () => {
    const [friendRows, inviteRows] = await Promise.all([
      client.listFriends(),
      client.listFriendInvites(),
    ]);
    setFriends(friendRows);
    setInvites(inviteRows);
  };

  const refreshAfterMutation = async (fallbackMessage: string) => {
    try {
      await refresh();
    } catch {
      setError(fallbackMessage);
    }
  };

  useEffect(() => {
    let alive = true;
    Promise.all([client.listFriends(), client.listFriendInvites()])
      .then(([friendRows, inviteRows]) => {
        if (alive) {
          setFriends(friendRows);
          setInvites(inviteRows);
        }
      })
      .catch((loadError: unknown) => {
        if (alive) setError(errorText(loadError));
      });
    return () => { alive = false; };
  }, [client]);

  useEffect(() => { if (created) createdHeading.current?.focus(); }, [created]);
  useEffect(() => { if (review) reviewHeading.current?.focus(); }, [review]);

  const createInvite = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const result = await client.createFriendInvite({
        endpoints,
        permissions,
        ttl_seconds: ttlSeconds,
        allow_private_endpoints: endpoints.some((endpoint) => endpointAddressClass(endpoint) === "private LAN"),
      });
      setCreated(result);
      setInvites((current) => [result.invite, ...current.filter((item) => item.invite_id !== result.invite.invite_id)]);
      await refreshAfterMutation("Invitation created, but the lists could not be refreshed. The displayed bearer link is still valid.");
    } catch (createError) {
      setError(errorText(createError));
    } finally {
      setBusy(false);
    }
  };

  const reviewInvite = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    setReview(null);
    setBusy(true);
    try {
      setReview(await client.reviewFriendInvite({
        link: reviewLink.trim(),
        allow_private_endpoints: allowLanReview,
      }));
    } catch (reviewError) {
      setError(errorText(reviewError));
    } finally {
      setBusy(false);
    }
  };

  const joinReviewedInvite = async () => {
    if (!review) return;
    setError("");
    setBusy(true);
    try {
      const result = await client.joinFriend({
        link: reviewLink.trim(),
        endpoint: review.endpoints[0],
        allow_private_endpoints: allowLanReview,
      });
      setFriends((current) => [
        result.friend,
        ...current.filter((item) => item.peer_id !== result.friend.peer_id),
      ]);
      if (result.endpoint_review_required) {
        setPendingJoin(result);
        notify("warn", "The inviter returned changed endpoints. Review them before activation.");
      } else {
        setPendingJoin(null);
        notify("ok", `${result.friend.display_name} joined Friend Mesh.`);
      }
      await refreshAfterMutation("Friendship was stored, but the lists could not be refreshed.");
    } catch (joinError) {
      setError(errorText(joinError));
    } finally {
      setBusy(false);
    }
  };

  const decideEndpointChange = async (approve: boolean) => {
    if (!pendingJoin) return;
    setBusy(true);
    setError("");
    try {
      const result = await client.reviewFriendEndpoints({
        peer_id: pendingJoin.friend.peer_id,
        approve,
        endpoints: pendingJoin.returned_endpoints,
      });
      setFriends((current) => [
        result.friend,
        ...current.filter((item) => item.peer_id !== result.friend.peer_id),
      ]);
      setPendingJoin(null);
      notify(approve ? "ok" : "warn", approve
        ? "Changed endpoints approved; friendship is active."
        : "Changed endpoints rejected; the local relationship credential was deleted.");
    } catch (reviewError) {
      setError(errorText(reviewError));
    } finally {
      setBusy(false);
    }
  };

  const togglePermission = (permission: FriendPermission) => {
    setPermissions((current) => current.includes(permission)
      ? current.filter((value) => value !== permission)
      : [...current, permission]);
  };

  const copyCreatedLink = async () => {
    if (!created || !navigator.clipboard) {
      notify("warn", "Clipboard access is unavailable; select the link text manually.");
      return;
    }
    await navigator.clipboard.writeText(created.link);
    notify("ok", "Invite link copied. Share it only with the intended person.");
  };

  return (
    <section className="friend-mesh" aria-labelledby="friend-mesh-title">
      <div className="friend-mesh-heading">
        <div>
          <span className="eyebrow">Friend Mesh</span>
          <h2 id="friend-mesh-title">Scoped access for people you know</h2>
          <p>Friend access is a separate authorization relationship. It never makes someone an identity trust root.</p>
        </div>
        <Chip tone="info">LAN / already-public V1</Chip>
      </div>

      {error ? <p className="friend-error" role="alert">{error}</p> : null}

      <div className="friend-mesh-grid">
        <Panel>
          <h3><UserPlus size={18} /> Create an invitation</h3>
          <form className="form-stack" onSubmit={(event) => void createInvite(event)}>
            <label className="field">
              <span>Advertised endpoints (one per line)</span>
              <textarea
                value={endpointsText}
                onChange={(event) => {
                  setEndpointsText(event.target.value);
                  setEndpointRiskAccepted(false);
                }}
                placeholder="https://node.example:8791"
                aria-describedby="friend-endpoint-help"
              />
            </label>
            <p id="friend-endpoint-help" className="muted friend-help">
              V1 does not provide NAT traversal. Use up to eight reviewed same-LAN or already-public endpoints.
            </p>
            {endpoints.length ? <EndpointList endpoints={endpoints} /> : null}
            <fieldset className="friend-permissions">
              <legend>Permission scope</legend>
              {permissionOptions.map((option) => (
                <label key={option.value}>
                  <input
                    type="checkbox"
                    checked={permissions.includes(option.value)}
                    onChange={() => togglePermission(option.value)}
                  />
                  {option.label} <code>{option.value}</code>
                </label>
              ))}
            </fieldset>
            <label className="field">
              <span>Expires after</span>
              <select value={ttlSeconds} onChange={(event) => setTtlSeconds(Number(event.target.value))}>
                <option value={900}>15 minutes</option>
                <option value={3600}>1 hour</option>
                <option value={21600}>6 hours</option>
                <option value={86400}>24 hours</option>
              </select>
            </label>
            <label className="friend-risk-check">
              <input
                type="checkbox"
                checked={endpointRiskAccepted}
                onChange={(event) => setEndpointRiskAccepted(event.target.checked)}
              />
              I reviewed every endpoint and understand the LAN/public reachability limit.
            </label>
            <Button
              type="submit"
              variant="primary"
              disabled={busy || !endpoints.length || endpoints.length > 8 || !permissions.length || hasInvalidEndpoint || !endpointRiskAccepted}
            >
              Create signed invitation
            </Button>
          </form>

          {created ? (
            <div className="friend-created" role="status">
              <h3 ref={createdHeading} tabIndex={-1}>Invitation created</h3>
              <p className="muted">The raw bearer link is shown only in this creation session.</p>
              <img
                className="friend-qr"
                src={friendInviteQrDataUrl(created.link)}
                alt={`QR code for invitation expiring ${formatTime(created.invite.expires_at)}`}
              />
              <label className="field">
                <span>One-use invitation link</span>
                <textarea readOnly value={created.link} onFocus={(event) => event.currentTarget.select()} />
              </label>
              <Button icon={Copy} onClick={() => void copyCreatedLink()}>Copy invitation</Button>
            </div>
          ) : null}
        </Panel>

        <Panel>
          <h3><QrCode size={18} /> Review a received invitation</h3>
          <form className="form-stack" onSubmit={(event) => void reviewInvite(event)}>
            <label className="field">
              <span>Paste invitation link</span>
              <textarea
                value={reviewLink}
                onChange={(event) => {
                  setReviewLink(event.target.value);
                  setReview(null);
                  setPendingJoin(null);
                }}
                placeholder="rynmesh://join/..."
              />
            </label>
            <label className="friend-risk-check">
              <input type="checkbox" checked={allowLanReview} onChange={(event) => {
                setAllowLanReview(event.target.checked);
                setReview(null);
                setPendingJoin(null);
              }} />
              Permit the local verifier to display private-LAN endpoints for explicit review.
            </label>
            <Button type="submit" disabled={busy || !reviewLink.trim()}>Verify and review offline</Button>
          </form>

          {review ? (
            <div className="friend-review" aria-live="polite">
              <h3 ref={reviewHeading} tabIndex={-1}>Verified invitation</h3>
              <dl className="friend-review-grid">
                <dt>Signature</dt><dd><Chip tone="ok">verified by local node</Chip></dd>
                <dt>Fingerprint</dt><dd><Hash value={review.verified_fingerprint} /></dd>
                <dt>Node</dt><dd>{review.node_name}</dd>
                <dt>Network</dt><dd><code>{review.network_id}</code></dd>
                <dt>Scope</dt><dd>{review.permissions.map((item) => <Chip key={item} tone="info">{item}</Chip>)}</dd>
                <dt>Expires</dt><dd>{formatTime(review.expires_at)}</dd>
                <dt>Endpoints</dt><dd><EndpointList endpoints={review.endpoints} /></dd>
              </dl>
              <p id="friend-join-note" className="friend-warning">
                No endpoint was contacted during review. Join now asks the local node to resolve and pin the selected endpoint, rotate the one-use secret, and persist the encrypted relationship.
              </p>
              <Button
                variant="primary"
                disabled={busy || Boolean(pendingJoin)}
                aria-describedby="friend-join-note"
                onClick={() => void joinReviewedInvite()}
              >
                Join Friend Mesh
              </Button>
              {pendingJoin ? (
                <div className="friend-endpoint-review" role="alert">
                  <h4>Endpoints changed during Join</h4>
                  <p>Original signed endpoints:</p>
                  <EndpointList endpoints={pendingJoin.original_endpoints} />
                  <p>Returned signed endpoints requiring a second decision:</p>
                  <EndpointList endpoints={pendingJoin.returned_endpoints} />
                  <div className="button-row">
                    <Button variant="primary" disabled={busy} onClick={() => void decideEndpointChange(true)}>
                      Approve exact endpoints
                    </Button>
                    <Button variant="danger" disabled={busy} onClick={() => void decideEndpointChange(false)}>
                      Reject and delete credential
                    </Button>
                  </div>
                </div>
              ) : null}
            </div>
          ) : null}
        </Panel>
      </div>

      <div className="friend-mesh-grid">
        <Panel>
          <h3>Outstanding invitations</h3>
          {invites.length === 0 ? <p className="muted">No invitations created on this node.</p> : (
            <ul className="friend-record-list">
              {invites.map((invite) => {
                const state = inviteState(invite);
                return (
                  <li key={invite.invite_id}>
                    <div>
                      <strong>{invite.permissions.join(", ")}</strong> <Chip tone={state.tone}>{state.label}</Chip>
                      <small>Expires {formatTime(invite.expires_at)} · {invite.endpoints.length} endpoint(s)</small>
                    </div>
                    <Button
                      disabled={state.label !== "active" || busy}
                      onClick={() => {
                        setBusy(true);
                        client.cancelFriendInvite(invite.invite_id)
                          .then(async (cancelled) => {
                            setInvites((current) => current.map((item) => item.invite_id === cancelled.invite_id ? cancelled : item));
                            if (created?.invite.invite_id === invite.invite_id) setCreated(null);
                            await refreshAfterMutation("Invitation cancelled, but the lists could not be refreshed.");
                          })
                          .catch((cancelError: unknown) => setError(errorText(cancelError)))
                          .finally(() => setBusy(false));
                      }}
                    >
                      Cancel
                    </Button>
                  </li>
                );
              })}
            </ul>
          )}
        </Panel>

        <Panel>
          <h3>Friends</h3>
          {friends.length === 0 ? <p className="muted">No friend relationships on this node.</p> : (
            <ul className="friend-record-list">
              {friends.map((friend) => (
                <li key={friend.peer_id}>
                  <div>
                    <strong>{friend.display_name}</strong> <Chip tone={friend.state === "active" ? "ok" : "danger"}>{friend.state}</Chip>
                    <Hash value={friend.peer_id} />
                    <small>Network {friend.network_id} · Last contact {formatTime(friend.last_contact_at)}</small>
                    <small>Granted: {friend.granted_permissions.join(", ") || "none"} · Received: {friend.received_permissions.join(", ") || "none"}</small>
                    <EndpointList endpoints={friend.reviewed_endpoints} />
                    <small>Revocation delivery: {friend.last_delivery_error ? `retry needed (${friend.last_delivery_error})` : friend.state === "revoked" ? "local denial active; remote delivery is best-effort" : "not applicable"}</small>
                  </div>
                  {friend.state === "revoked" && friend.last_delivery_error ? (
                    <Button
                      disabled={busy}
                      onClick={async () => {
                        setBusy(true);
                        setError("");
                        try {
                          const result = await client.retryFriendRevocation(friend.peer_id);
                          notify(result.delivery === "delivered" ? "ok" : "warn", result.delivery === "delivered"
                            ? "Signed revocation delivered."
                            : "Friend is still unreachable; local denial remains active.");
                          await refreshAfterMutation("Delivery was retried, but the friend list could not be refreshed.");
                        } catch (retryError) {
                          setError(errorText(retryError));
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      Retry signed notice
                    </Button>
                  ) : (
                    <Button
                      variant="danger"
                      icon={UserMinus}
                      disabled={friend.state !== "active" || busy}
                      onClick={() => confirm({
                      title: `Revoke ${friend.display_name}?`,
                      body: "Local authorization and its relationship secret are removed first. Remote notification is best-effort and may remain pending while the friend is offline.",
                      risk: "high",
                      confirmLabel: "Revoke access now",
                      details: [
                        { label: "Peer fingerprint", value: friend.peer_id },
                        { label: "Permissions", value: friend.granted_permissions.join(", ") || "none" },
                      ],
                      onConfirm: async () => {
                        setBusy(true);
                        try {
                          await client.revokeFriend(friend.peer_id);
                          setFriends((current) => current.map((item) => item.peer_id === friend.peer_id
                            ? { ...item, state: "revoked", revoked_at: new Date().toISOString() }
                            : item));
                          notify("warn", "Local friend access revoked immediately; remote notice is best-effort.");
                          await refreshAfterMutation("Friend access was revoked locally, but the lists could not be refreshed.");
                        } catch (revokeError) {
                          setError(errorText(revokeError));
                        } finally {
                          setBusy(false);
                        }
                      },
                      })}
                    >
                      Revoke
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>

      <p className="friend-boundary"><ShieldAlert size={16} /> Friend Mesh does not modify trust roots, ranking, quarantine, or registry identity policy.</p>
    </section>
  );
}
