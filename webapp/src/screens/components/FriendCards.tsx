import { useEffect, useState } from "react";
import { useAppContext } from "../../appContext";
import { Button, Panel } from "../../components/ui";
import ContentViewer from "../../components/ContentViewer";
import { friendsApi } from "../../domain/friendsClient";
import { contentFromHistory } from "../../domain/readingHistory";
import { digestApi } from "../../domain/digestClient";
import type { FriendContentCard, FriendRecord } from "../../domain/friendTypes";
import type { ContentItem } from "../../domain/types";

export default function FriendCards({ friends = [] }: { friends?: FriendRecord[] }) {
  const { client, confirm } = useAppContext();
  const [cards, setCards] = useState<FriendContentCard[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState<ContentItem | null>(null);
  const refresh = async () => { setCards((await friendsApi.cards()).cards); setLoaded(true); };
  useEffect(() => {
    let active = true, running = false;
    const poll = async () => {
      if (running) return;
      running = true;
      try { const result = await friendsApi.cards(); if (active) { setCards(result.cards); setLoaded(true); } }
      catch { if (active) setError("Could not refresh shared content. Retry when the node is available."); }
      finally { running = false; }
    };
    void poll(); const timer = window.setInterval(() => void poll(), 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  const act = async (operation: () => Promise<void>) => {
    setBusy(true); setError("");
    try { await operation(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Could not open this shared document."); await refresh().catch(() => undefined); }
    finally { setBusy(false); }
  };
  const open = async (card: FriendContentCard, repair = false) => {
    const imported = await friendsApi.fetchCard(card.card_id, repair);
    const history = await digestApi.listConsumption();
    const record = history.find((row) => row.item_id === imported.library_id);
    if (!record) throw new Error("Document was saved, but its reading entry could not be loaded. Retry opening it.");
    setReading(contentFromHistory(record)); await refresh();
  };
  return <Panel><h2>Shared content</h2>
    <p>Cards show metadata first. Choose Download and read to save a private copy. Removing a friend cannot recall copies already saved.</p>
    {error ? <p role="alert">{error}</p> : null}
    <Button disabled={busy} onClick={() => void act(refresh)}>Refresh shared content</Button>
    {!loaded ? <p>Loading shared content…</p> : !cards.length ? <p>No shared content yet. Open an article and choose Share with a friend.</p> : cards.map((card) => <article key={card.card_id}>
      <h3>{card.card.title}</h3><p>{card.card.summary}</p>
      <p>{card.dir === "out" ? "Shared by you" : `Shared by ${friends.find((friend) => friend.peer_id === card.from)?.node_name ?? "a friend"}`} · {card.card.source || "Source not supplied"}</p>
      {card.dir !== "out" ? <details><summary>Sender identity</summary><span style={{ overflowWrap: "anywhere" }}>{card.from}</span></details> : null}
      {card.card.publisher_peer_id ? <p>Publisher: {card.card.publisher_peer_id}</p> : null}
      {card.card.source_url ? <p style={{ overflowWrap: "anywhere" }}>Source: {card.card.source_url}</p> : null}
      {card.dir === "out" ? <><p>{card.delivery_state === "delivered" ? "Card received · confirmed" : card.delivery_state === "mailbox" ? "In encrypted mailbox · waiting for confirmation" : card.delivery_state === "expired" ? "Expired · not delivered" : card.delivery_state === "failed" ? "Could not deliver · retry available" : "Waiting for delivery"}</p>
        {["queued", "mailbox", "failed"].includes(card.delivery_state ?? "") ? <Button disabled={busy} onClick={() => void act(async () => { await friendsApi.retryCard(card.card_id); await refresh(); })}>Retry this card</Button> : null}</>
        : card.fetch_state === "fetched" ? <Button disabled={busy} onClick={() => void act(() => open(card))}>Read saved copy</Button>
        : card.fetch_state === "unavailable" ? <><p>Saved copy unavailable.</p><Button disabled={busy} onClick={() => void act(() => open(card, true))}>Download again</Button></>
        : card.card.fetch_available ? <Button disabled={busy} onClick={() => void act(() => open(card))}>Download and read ({card.card.size_bytes} bytes)</Button>
        : <p>Metadata only. No private document is available.</p>}
      {card.fetch_state === "fetched" && card.fetched_library_id?.startsWith("import:") ? <Button disabled={busy} onClick={() => confirm({
        title: "Remove this local copy?", body: "This removes the downloaded document from this node. The card and bookmark remain. Downloading again requires access to your friend's copy.",
        risk: "medium", confirmLabel: "Remove local copy", onConfirm: async () => { await friendsApi.removeDocument(card.fetched_library_id!.slice(7)); await refresh().catch(() => setError("The copy was removed. Refresh shared content to update this list.")); },
      })}>Remove local copy</Button> : null}
    </article>)}
    {reading ? <ContentViewer item={reading} client={client} onClose={() => setReading(null)} onRead={() => client.recordContentConsumption(reading, "opened")} /> : null}
  </Panel>;
}
