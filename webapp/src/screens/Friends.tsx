import QRCode from "qrcode";
import { Activity, UserPlus } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { useCallback, useEffect, useRef, useState } from "react";
import { useAppContext } from "../appContext";
import { runThenReload } from "../domain/actThenReload";
import type { FriendContentCard, FriendInvitePreview, FriendInviteResult, FriendRecord } from "../domain/friendTypes";
import { extractInvite, friendsApi, invitationText } from "../domain/friendsClient";
import { tr, uiLocale, useUILanguage } from "../uiI18n";
import AddFriendDialog, { type InviteTab } from "./components/AddFriendDialog";
import FriendAI from "./components/FriendAI";
import FriendCards, { cardBelongsTo } from "./components/FriendCards";
import FriendConnectionDiagnostics from "./components/FriendConnectionDiagnostics";
import FriendConversation from "./components/FriendConversation";
import { Banner, CopyButton, EmptyFriends, FriendAvatar, FriendBand, FriendRow, LinkPill, LinkedText, PageNote, RefreshIcon, SearchField, friendHue, shortPeerId } from "./components/FriendsParts";
import { errorText, linkInfo } from "./components/friendLinks";
import { useFriendDiagnostics } from "./components/useFriendDiagnostics";
import "./friends-refresh.css";

type FriendTab = "msgs" | "cards" | "ai" | "conn";

export default function Friends() {
  useUILanguage();
  const [params] = useSearchParams();
  const { confirm } = useAppContext();
  const [friends, setFriends] = useState<FriendRecord[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [invite, setInvite] = useState<FriendInviteResult | null>(null);
  const [reach, setReach] = useState<{ endpoint: string; address_category: string } | null>(null);
  const [reachError, setReachError] = useState("");
  const refreshReach = useCallback(async () => {
    setReach(null); setReachError("");
    try { setReach(await friendsApi.invitationContext()); }
    catch { setReachError(tr("Could not load your sharing address. Check the node's sharing address and retry.")); }
  }, []);
  useEffect(() => { void refreshReach(); }, [refreshReach]);
  const [qr, setQr] = useState("");
  const [paste, setPaste] = useState("");
  const [review, setReview] = useState<{ uri: string; preview: FriendInvitePreview } | null>(null);
  const revision = useRef(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [notice, setNotice] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [inviteTab, setInviteTab] = useState<InviteTab>("create");
  const [tab, setTab] = useState<FriendTab>("msgs");
  const [query, setQuery] = useState("");
  const [cards, setCards] = useState<FriendContentCard[]>([]);
  const [allowed, setAllowed] = useState<string[] | null>(null);
  const [copied, setCopied] = useState(false);
  const load = useCallback(async () => {
    const [result, invitations] = await Promise.all([friendsApi.list(), friendsApi.invites()]);
    setFriends(result.friends); setLoaded(true); setLoadError("");
    setInvite((current) => {
      const stored = invitations.invites.find((row) => row.invite_id === current?.invite.invite_id);
      return stored && (stored.status !== "active" || Date.parse(stored.expires_at) <= Date.now()) ? null : current;
    });
  }, []);
  const act = async (operation: () => Promise<void>) => {
    setBusy(true); setError(""); setNotice("");
    try { await operation(); }
    catch (cause) { setError(errorText(cause, tr("Could not confirm this operation. Retry."))); }
    finally { setBusy(false); }
  };
  // A confirmed removal followed by a failed reload is not a failed removal:
  // nesting load() inside the operation would report the node's work as an error.
  const actThenReload = (operation: () => Promise<unknown>) => act(() => runThenReload(operation, load, {
    onError: (cause) => { throw cause; }, onNotice: setNotice,
  }));
  useEffect(() => {
    let active = true;
    let running = false;
    const poll = async () => {
      if (running) return;
      running = true;
      try { await load(); }
      catch { if (active) setLoadError(tr("Could not load friends. Retry to restore your list.")); }
      finally { running = false; }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [load]);
  useEffect(() => {
    let active = true;
    setQr("");
    if (invite) void QRCode.toDataURL(invite.invite_uri, { width: 512, margin: 4 })
      .then((value) => { if (active) setQr(value); })
      .catch(() => { if (active) setNotice(tr("QR code unavailable. Copy the invitation instead.")); });
    return () => { active = false; };
  }, [invite]);
  const activeFriends = friends.filter((friend) => friend.status === "active");
  const pendingRemovals = friends.filter((friend) => friend.status === "revoked" && friend.revocation_delivery === "pending");
  const undeliverable = friends.filter((friend) => friend.status === "revoked" && friend.revocation_delivery === "undeliverable");
  const diag = useFriendDiagnostics(activeFriends, { loadOnMount: true });
  const urlPeer = params.get("peer");
  useEffect(() => {
    setSelected(params.get("peer"));
    if (params.get("card")) setTab("cards");
    else if (params.get("peer") || params.get("message")) setTab("msgs");
  }, [params]);
  const picked = activeFriends.find((friend) => friend.peer_id === selected);
  const current = picked ?? (selected && selected === urlPeer ? undefined : activeFriends[0]);
  const linkOf = (friend: FriendRecord) => linkInfo(diag.result?.links.find((link) => link.relationship_id === friend.relationship_id)?.state ?? "not_checked");
  const needle = query.trim().toLowerCase();
  const rows = needle ? activeFriends.filter((friend) => [friend.node_name, friend.endpoint].some((value) => value.toLowerCase().includes(needle))) : activeFriends;
  const hasRows = activeFriends.length + pendingRemovals.length + undeliverable.length > 0;
  const showEmpty = loaded && activeFriends.length === 0;
  const verified = diag.result ? activeFriends.filter((friend) => linkOf(friend).tone === "ok").length : null;
  const usingAi = allowed ? activeFriends.filter((friend) => allowed.includes(friend.relationship_id)).length : null;
  const sharedCount = current ? cards.filter((card) => cardBelongsTo(card, current)).length : 0;
  const pageError = addOpen ? "" : error || diag.error;
  const pageNotice = addOpen ? "" : notice || diag.notice;
  const since = current ? new Date(current.created_at) : null;

  const create = () => void act(async () => {
    try { setInvite(await friendsApi.createInvite(reach!.endpoint)); }
    catch (cause) { await refreshReach(); throw cause; }
  });
  const askRemove = (friend: FriendRecord) => confirm({ title: tr("Remove {{name}}?", { name: friend.node_name }),
    body: tr("New messages and private content requests are blocked immediately on this device. The other device may receive the notice later. Copies they already saved cannot be recalled."),
    risk: "high", confirmLabel: tr("Remove friend"), onConfirm: () => actThenReload(() => friendsApi.revoke(friend.relationship_id)) });

  return <div className="rf rd-friends">
    <header className="rq-head">
      <div className="rq-head-text">
        <h1>{tr("Friends")}</h1>
        <p>{tr("Invite someone you know, review their identity, then share your first message.")}</p>
      </div>
      <div className="rq-head-actions">
        <button type="button" className="rf-btn" disabled={busy || diag.busy || !diag.selected.length} onClick={() => { setTab("conn"); void diag.check(diag.selected); }}><Activity size={15} strokeWidth={1.6} aria-hidden="true" />{tr("Check connections")}</button>
        <button type="button" className="rf-btn rf-btn-p" onClick={() => setAddOpen(true)}><UserPlus size={15} strokeWidth={2} aria-hidden="true" />{tr("Add a friend")}</button>
      </div>
    </header>
    {pageError || loadError ? <div className="rq-banner rq-banner-bad" role="alert"><p>{pageError || loadError}</p><button type="button" className="rf-btn rf-btn-sm" disabled={busy} onClick={() => void act(load)}>{tr("Refresh friends")}</button></div> : null}
    {pageNotice ? <p className="rq-banner rq-banner-ok" role="status">{pageNotice}</p> : null}
    {selected && selected === urlPeer && loaded && !picked ? <Banner tone="warn" role="alert"><p>{tr("This friend is unavailable or access was removed.")}</p></Banner> : null}

    <FriendBand friends={activeFriends.length} verified={verified} usingAi={usingAi} />

    <div className="rq-cols">
      {!loaded || hasRows ? <section className="rq-list rf-card" aria-labelledby="rq-list-title">
        <div className="rq-list-head">
          <h2 id="rq-list-title">{tr("Your friends")}</h2>
          <button type="button" className="rf-ib" aria-label={tr("Refresh friends")} title={tr("Refresh friends")} disabled={busy} onClick={() => void act(load)}><RefreshIcon /></button>
        </div>
        <div className="rq-list-search"><SearchField value={query} onChange={setQuery} label={tr("Search friends")} placeholder={tr("Search friends")} /></div>
        <div className="rq-list-body">
          {!loaded ? <p className="rq-loading">{tr("Loading friends…")}</p> : null}
          {rows.map((friend) => <FriendRow key={friend.relationship_id} friend={friend} info={linkOf(friend)} usesAi={allowed?.includes(friend.relationship_id) === true}
            selected={friend.relationship_id === current?.relationship_id} onSelect={() => setSelected(friend.peer_id)} />)}
          {loaded && activeFriends.length > 0 && rows.length === 0 ? <p className="rq-none-text">{tr("No friends match this search.")}</p> : null}
          {pendingRemovals.map((friend) => <div className="rq-removed rf-t-warn" key={friend.relationship_id}>
            <p>{tr("{{name}}: removed locally; waiting to notify their device.", { name: friend.node_name })}</p>
            <button type="button" className="rf-btn rf-btn-sm" disabled={busy} onClick={() => void actThenReload(() => friendsApi.retryRevocation(friend.relationship_id))}>{tr("Retry removal notice")}</button>
          </div>)}
          {undeliverable.map((friend) => <div className="rq-removed rf-t-off" key={friend.relationship_id}>
            <p>{tr("{{name}}: Removed on this device. The removal notice could not be sent because this friend's credentials were no longer available; they will see an error on their next request.", { name: friend.node_name })}</p>
          </div>)}
        </div>
      </section> : null}

      <section className={`rq-detail rf-card rf-h-${current ? friendHue(current) : "slate"}`} aria-label={current?.node_name ?? tr("Friend")} hidden={!current}>
        {current ? <header className="rq-detail-head">
          <FriendAvatar friend={current} size="lg" />
          <div className="rq-detail-id">
            <h2>{current.node_name}</h2>
            <div className="rq-detail-sub"><LinkPill info={linkOf(current)} />{since && !Number.isNaN(since.getTime()) ? <small>{tr("Friends since {{date}}", { date: since.toLocaleDateString(uiLocale(), { month: "short", day: "numeric" }) })}</small> : null}</div>
          </div>
        </header> : null}
        <div className="rq-tabs">
          <div className="rf-seg" role="group" aria-label={tr("Friend sections")}>
            {([["msgs", tr("Messages")], ["cards", tr("Shared content")], ["ai", tr("AI")], ["conn", tr("Connection")]] as const).map(([id, label]) =>
              <button key={id} type="button" aria-pressed={tab === id} onClick={() => setTab(id)}>{label}{id === "cards" && sharedCount ? <em>{sharedCount}</em> : null}</button>)}
          </div>
        </div>
        <div className="rq-pane" data-on={tab === "msgs"}>
          {current ? <FriendConversation key={current.relationship_id} friend={current} focusMessage={params.get("message")} /> : null}
        </div>
        <div className="rq-pane" data-on={tab === "cards"}>
          <FriendCards friends={friends} friend={current ?? null} focusCard={params.get("card")} onLoaded={setCards} />
        </div>
        <div className="rq-pane" data-on={tab === "ai"}>
          <FriendAI friends={friends} friend={current ?? null} onAllowed={setAllowed} />
        </div>
        <div className="rq-pane rq-pane-conn" data-on={tab === "conn"}>
          <FriendConnectionDiagnostics friends={activeFriends} focus={current ?? null} diag={diag} />
          {current ? <>
            <dl className="rq-facts">
              <div><dt>{tr("Identity")}</dt><dd><code className="rf-code-chip" title={current.peer_id}>{shortPeerId(current.peer_id)}</code>
                <CopyButton value={current.peer_id} label={tr("Copy identity")} copied={copied} onCopied={() => { setCopied(true); window.setTimeout(() => setCopied(false), 1600); }} /></dd></div>
              <div><dt>{tr("Address")}</dt><dd className="rf-mono">{current.endpoint}</dd></div>
              <div><dt>{tr("Allows")}</dt><dd>{tr("Messages, attachments up to 5 MiB, content cards")}</dd></div>
              <div><dt>{tr("Stays off")}</dt><dd>{tr("VPN, access to your files and credits. AI only if you allow it.")}</dd></div>
            </dl>
            <div className="rq-danger">
              <div><b>{tr("Remove friend")}</b><small>{tr("New messages and private content requests are blocked immediately on this device. The other device may receive the notice later. Copies they already saved cannot be recalled.")}</small></div>
              <button type="button" className="rf-btn rf-btn-d rf-btn-sm" disabled={busy} onClick={() => askRemove(current)}>{tr("Remove friend")}</button>
            </div>
          </> : null}
        </div>
      </section>

      {showEmpty ? <EmptyFriends onAdd={() => setAddOpen(true)} /> : null}
    </div>

    <PageNote><LinkedText text={tr("Messages with your own computers are in [link]Messages[/link].")} to="/chat" /></PageNote>

    {addOpen ? <AddFriendDialog tab={inviteTab} onTab={setInviteTab} onClose={() => setAddOpen(false)} busy={busy} error={error} notice={notice}
      reach={reach} reachError={reachError} onRefreshReach={() => void act(refreshReach)} invite={invite} qr={qr}
      onCreate={create}
      onCopy={() => void act(async () => { await navigator.clipboard.writeText(invitationText(invite!)); setNotice(tr("Invitation and installation instructions copied.")); })}
      onCancelInvite={() => void act(async () => { await friendsApi.cancel(invite!.invite.invite_id); setInvite(null); setNotice(tr("Invitation cancelled.")); })}
      paste={paste} onPaste={(value) => { revision.current += 1; setPaste(value); setReview(null); }} review={review}
      onReview={() => void act(async () => {
        const version = revision.current; const uri = extractInvite(paste);
        const preview = await friendsApi.inspect(uri);
        if (version === revision.current) setReview({ uri, preview });
      })}
      onJoin={() => void act(async () => {
        const version = revision.current;
        const friend = await friendsApi.join(review!.uri);
        if (version === revision.current) { setPaste(""); setReview(null); }
        setSelected(friend.peer_id); setTab("msgs"); setAddOpen(false); setNotice(tr("Friend added. Send your first message below.")); await load();
      })} /> : null}
  </div>;
}
