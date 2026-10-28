import { RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useAppContext } from "../../appContext";
import ContentViewer from "../../components/ContentViewer";
import FriendCardCleanupPanel from "./FriendCardCleanupPanel";
import { friendDeliveryExplanation, friendsApi } from "../../domain/friendsClient";
import { libraryCleanup, libraryCleanupScope, libraryReviewCounts } from "../../domain/libraryCleanup";
import { contentFromHistory } from "../../domain/readingHistory";
import { digestApi } from "../../domain/digestClient";
import type { FriendContentCard, FriendMessage, FriendRecord } from "../../domain/friendTypes";
import type { ContentItem } from "../../domain/types";
import type { Tone } from "../../personal/drop/FamilyParts";
import { tr, useUILanguage } from "../../uiI18n";
import { errorText } from "./friendLinks";
import { Tinted } from "./FriendsParts";
import "../friends-refresh.css";

const deliveryTone: Record<NonNullable<FriendMessage["delivery_state"]>, Tone> = { queued: "off", sending: "sky", mailbox: "warn", delivered: "ok", failed: "bad", expired: "off" };
function deliveryLabel(card: FriendContentCard): string {
  switch (card.delivery_state) {
    case "delivered": return tr("Card received · confirmed");
    case "mailbox": return tr("In encrypted mailbox · waiting for confirmation");
    case "expired": return tr("Expired · delivery unconfirmed");
    case "failed": return tr("Could not confirm delivery · retry available");
    default: return tr("Waiting for delivery");
  }
}

/** A card belongs to the friend it was received from or sent to. */
export const cardBelongsTo = (card: FriendContentCard, friend: Pick<FriendRecord, "peer_id">) => (card.dir === "out" ? card.to === friend.peer_id : card.from === friend.peer_id);

export default function FriendCards({ friends = [], focusCard, friend, onLoaded }: {
  friends?: FriendRecord[]; focusCard?: string | null;
  /** Show only this friend's cards (the focused card is always shown). Without it every friend's cards are listed. */
  friend?: FriendRecord | null;
  onLoaded?: (cards: FriendContentCard[]) => void;
}) {
  useUILanguage();
  const { client, confirm } = useAppContext();
  const [cards, setCards] = useState<FriendContentCard[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState<ContentItem | null>(null);
  const loadVersion = useRef(0);
  const report = useRef(onLoaded);
  report.current = onLoaded;
  useEffect(() => {
    if (!loaded || !focusCard) return;
    const element = document.getElementById(`friend-card-${focusCard}`);
    element?.focus({ preventScroll: true }); element?.scrollIntoView?.({ block: "center" });
  }, [loaded, focusCard]);
  const refresh = async () => {
    const version = ++loadVersion.current;
    const result = await friendsApi.cards();
    if (version !== loadVersion.current) return;
    setCards(result.cards); setLoaded(true); setLoadError(""); report.current?.(result.cards);
  };
  useEffect(() => {
    let active = true, running = false;
    const poll = async () => {
      if (running) return;
      running = true;
      const version = ++loadVersion.current;
      try { const result = await friendsApi.cards(); if (active && version === loadVersion.current) { setCards(result.cards); setLoaded(true); setLoadError(""); report.current?.(result.cards); } }
      catch { if (active && version === loadVersion.current) setLoadError(tr("Could not refresh shared content. Retry when the node is available.")); }
      finally { running = false; }
    };
    void poll(); const timer = window.setInterval(() => void poll(), 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  const act = async (operation: () => Promise<void>) => {
    setBusy(true); setError("");
    try { await operation(); }
    catch (cause) { setError(errorText(cause, tr("Could not open this shared document."))); await refresh().catch(() => undefined); }
    finally { setBusy(false); }
  };
  const open = async (card: FriendContentCard, repair = false) => {
    const imported = await friendsApi.fetchCard(card.card_id, repair);
    const history = await digestApi.listConsumption();
    const record = history.find((row) => row.item_id === imported.library_id);
    if (!record) throw new Error(tr("Document was saved, but its reading entry could not be loaded. Retry opening it."));
    setReading(contentFromHistory(record)); await refresh();
  };
  const shown = friend ? cards.filter((card) => cardBelongsTo(card, friend) || card.card_id === focusCard) : cards;
  return <div className="rq-cards">
    <div className="rq-cards-head">
      <p className="rq-help">{tr("Cards show metadata first. Choose Download and read to save a private copy. Removing a friend cannot recall copies already saved.")}</p>
      <button type="button" className="rf-ib rf-ib-sm" aria-label={tr("Refresh shared content")} title={tr("Refresh shared content")} disabled={busy} onClick={() => void act(refresh)}><RefreshCw size={14} strokeWidth={1.7} aria-hidden="true" /></button>
    </div>
    {error || loadError ? <p className="rq-banner rq-banner-bad" role="alert">{error || loadError}</p> : null}
    {!loaded ? <p className="rq-loading">{tr("Loading shared content…")}</p> : !shown.length ? <p className="rq-none-text">{tr("No shared content yet. Open an article and choose Share with a friend.")}</p> : shown.map((card) => {
      const out = card.dir === "out";
      const sender = friends.find((candidate) => candidate.peer_id === card.from)?.node_name ?? tr("a friend");
      const explanation = out ? friendDeliveryExplanation(card) : "";
      return <article key={card.card_id} id={`friend-card-${card.card_id}`} tabIndex={-1} className="rq-card" data-focus={card.card_id === focusCard}>
        <span className="rq-card-ico rf-h-violet" aria-hidden="true"><svg width="20" height="20" viewBox="0 0 24 24"><rect x="5" y="3" width="14" height="18" rx="2.5" fill="#fff" /><rect className="rd-deep" x="8" y="7.5" width="8" height="1.6" rx=".8" fillOpacity=".5" /><rect className="rd-deep" x="8" y="11" width="8" height="1.6" rx=".8" fillOpacity=".35" /><rect className="rd-deep" x="8" y="14.5" width="5" height="1.6" rx=".8" fillOpacity=".35" /></svg></span>
        <div className="rq-card-body">
          <h3>{card.card.title}</h3>
          <p>{card.card.summary}</p>
          {card.card.content_truncated ? <small>{tr("This shared text is shortened; it does not include the full source.")}</small> : null}
          <small>{out ? tr("Shared by you") : tr("Shared by {{name}}", { name: sender })} · {card.card.source || tr("Source not supplied")}</small>
          {!out ? <details><summary>{tr("Sender identity")}</summary><span className="rf-mono">{card.from}</span></details> : null}
          {card.card.publisher_peer_id ? <small>{tr("Publisher: {{id}}", { id: card.card.publisher_peer_id })}</small> : null}
          {card.card.source_url ? <small className="rq-wrap">{tr("Source: {{url}}", { url: card.card.source_url })}</small> : null}
          {explanation ? <small role="status" className="rq-explain">{tr(explanation)}</small> : null}
          {!out && card.fetch_state === "unavailable" ? <small className="rq-warn-text">{tr("Saved copy unavailable.")}</small> : null}
          {!out && card.fetch_state !== "fetched" && card.fetch_state !== "unavailable" && !card.card.fetch_available ? <small>{tr("Metadata only. No private document is available.")}</small> : null}
        </div>
        <div className="rq-card-actions">
          {out ? <>
            <Tinted tone={deliveryTone[card.delivery_state ?? "queued"]} className="rq-status"><i aria-hidden="true" />{deliveryLabel(card)}</Tinted>
            {["queued", "mailbox", "failed"].includes(card.delivery_state ?? "") ? <button type="button" className="rf-btn rf-btn-sm" disabled={busy} onClick={() => void act(async () => { await friendsApi.retryCard(card.card_id); await refresh(); })}>{tr("Retry this card")}</button> : null}
          </> : card.fetch_state === "fetched" ? <button type="button" className="rf-btn rf-btn-sm" disabled={busy} onClick={() => void act(() => open(card))}>{tr("Read saved copy")}</button>
            : card.fetch_state === "unavailable" ? <button type="button" className="rf-btn rf-btn-sm" disabled={busy} onClick={() => void act(() => open(card, true))}>{tr("Download again")}</button>
            : card.card.fetch_available ? <button type="button" className="rf-btn rf-btn-p rf-btn-sm" disabled={busy} onClick={() => void act(() => open(card))}>{tr("Download and read ({{size}} bytes)", { size: card.card.size_bytes ?? 0 })}</button>
            : null}
          {card.fetch_state === "fetched" && card.fetched_library_id?.startsWith("import:") ? <button type="button" className="rf-btn rf-btn-sm" disabled={busy} onClick={() => void act(async () => {
            const reviewed = await libraryCleanup.preview(card.fetched_library_id!.slice(7));
            confirm({ title: tr("Remove this local copy?"), body: libraryReviewCounts(reviewed) + " " + libraryCleanupScope,
              risk: "high", confirmLabel: tr("Remove local copy"), onConfirm: () => act(async () => {
                try { await libraryCleanup.begin(reviewed); }
                catch (cause) { throw new Error(errorText(cause, tr("Removal was not confirmed.")) + " " + tr("Check document cleanup progress in Settings → Privacy & data.")); }
                await refresh();
              }),
            });
          })}>{tr("Remove local copy")}</button> : null}
        </div>
      </article>;
    })}
    <FriendCardCleanupPanel onChanged={() => { void refresh().catch(() => setLoadError(tr("Could not refresh shared content. Retry to see the current list."))); }} />
    {reading ? <ContentViewer item={reading} client={client} onClose={() => setReading(null)} onRead={() => client.recordContentConsumption(reading, "opened")} /> : null}
  </div>;
}
