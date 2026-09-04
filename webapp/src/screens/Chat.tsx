import { BookOpen, CheckCircle2, Download, Paperclip, RefreshCw, SendHorizontal, ShieldCheck, UserPlus } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useAppContext } from "../appContext";
import { Button, LoadingPanel, PageHeader, Panel } from "../components/ui";
import type { FriendContentCard, FriendMessage, FriendRecord } from "../domain/friendTypes";
import type { LibraryItem } from "../domain/libraryTypes";

function attachmentBase(streamUrl: string): string { return streamUrl.replace(/\/stream$/, ""); }
function formatTime(ts?: string): string {
  if (!ts) return "";
  const value = new Date(ts);
  return Number.isNaN(value.getTime()) ? ts : value.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
function formatBytes(value?: number): string {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}
async function fileToBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer()); let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

export default function Chat() {
  const { client, confirm, notify } = useAppContext();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [friends, setFriends] = useState<FriendRecord[]>([]);
  const [selected, setSelected] = useState<FriendRecord | null>(null);
  const [messages, setMessages] = useState<FriendMessage[]>([]);
  const [cards, setCards] = useState<FriendContentCard[]>([]);
  const [library, setLibrary] = useState<LibraryItem[]>([]);
  const [shareId, setShareId] = useState("");
  const [text, setText] = useState("");
  const [attachment, setAttachment] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [fetchingCardId, setFetchingCardId] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const selectedRef = useRef<FriendRecord | null>(null);
  selectedRef.current = selected;
  const msgBase = attachmentBase(client.messagesStreamUrl());

  useEffect(() => { void Promise.all([client.listFriends(), client.listFriendContentCards(), client.listLibrary({ view: "all", limit: 100 })]).then(([friendRows, cardRows, libraryPage]) => {
    const active = friendRows.filter((row) => row.status === "active");
    setFriends(active); setCards(cardRows); setLibrary(libraryPage.items);
    const requested = params.get("friend"); setSelected(active.find((row) => row.peer_id === requested) ?? active[0] ?? null);
  }); }, [client, params]);

  useEffect(() => {
    if (!selected) { setMessages([]); return; }
    let alive = true; setLoading(true);
    void client.listFriendMessages(selected.peer_id).then((rows) => { if (alive) setMessages(rows); }).catch(() => { if (alive) setMessages([]); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [client, selected]);

  useEffect(() => {
    const source = new EventSource(client.messagesStreamUrl());
    source.onmessage = (event) => {
      try {
        const row = JSON.parse(event.data) as FriendMessage; const open = selectedRef.current;
        if (!open || (row.from !== open.peer_id && row.to !== open.peer_id)) return;
        setMessages((current) => current.some((item) => item.msg_id === row.msg_id) ? current.map((item) => item.msg_id === row.msg_id ? row : item) : [...current, row]);
      } catch { /* malformed stream records are ignored */ }
    };
    return () => source.close();
  }, [client]);
  useEffect(() => { if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight; }, [messages]);

  const send = async () => {
    if (!selected || (!text.trim() && !attachment)) return;
    if (attachment && attachment.size > 5 * 1024 * 1024) { notify("danger", "Attachments are limited to 5 MiB"); return; }
    setSending(true);
    try {
      const request: { peer_id: string; text?: string; attachment?: { filename: string; mime: string; bytes_b64: string } } = { peer_id: selected.peer_id, text: text.trim() };
      if (attachment) request.attachment = { filename: attachment.name, mime: attachment.type || "application/octet-stream", bytes_b64: await fileToBase64(attachment) };
      const row = await client.sendFriendMessage(request); setMessages((current) => [...current, row]); setText(""); setAttachment(null); if (inputRef.current) inputRef.current.value = "";
    } catch (cause) { notify("danger", cause instanceof Error ? cause.message : "Message could not be sent"); }
    finally { setSending(false); }
  };
  const share = async () => {
    const item = library.find((row) => row.library_id === shareId); if (!selected || !item) return;
    const result = await client.sendFriendContentCard({ peer_id: selected.peer_id, card: { library_id: item.library_id, title: item.title, summary: item.summary, kind: item.kind, source: item.source.title } });
    notify(result.delivered ? "ok" : "warn", result.delivered ? "Content card delivered" : "Friend is offline; try again later");
  };
  const reviewAndFetch = (row: FriendContentCard) => {
    const card = row.card;
    const sender = friends.find((friend) => friend.peer_id === row.from)?.node_name ?? "Paired friend";
    confirm({
      title: `Get ${card.title}?`,
      body: "Only metadata has been received so far. After you confirm, Ryn will fetch the sealed file from this paired friend, verify its SHA-256 hash, then save a managed copy in My Content.",
      risk: "medium",
      confirmLabel: "Get and save",
      details: [
        { label: "Sent by", value: `${sender} · ${row.from}` },
        { label: "Publisher", value: card.publisher_peer_id || "Not declared" },
        { label: "Source", value: card.source || "Not declared" },
        { label: "File", value: `${card.filename || card.title} · ${card.mime || card.kind} · ${formatBytes(card.size_bytes)}` },
        { label: "Integrity", value: card.sha256 ? `SHA-256 ${card.sha256}` : "No downloadable body advertised" },
      ],
      onConfirm: async () => {
        setFetchingCardId(row.card_id);
        try {
          const result = await client.fetchFriendContentCard(row.card_id);
          setCards((current) => current.map((item) => item.card_id === row.card_id ? {
            ...item,
            fetch_state: "fetched",
            fetched_library_id: result.library_id,
            sha256_verified: result.sha256_verified,
          } : item));
          const page = await client.listLibrary({ view: "all", limit: 100 });
          setLibrary(page.items);
          notify("ok", "Hash verified; content saved to My Content");
        } catch (cause) {
          notify("danger", cause instanceof Error ? cause.message : "Content could not be fetched safely");
        } finally {
          setFetchingCardId("");
        }
      },
    });
  };

  return <div className="screen-stack">
    <PageHeader eyebrow="Friends" title="Private messages" context="Only explicitly paired people appear here. Shared content arrives as metadata first; its body is fetched only after you review and confirm." />
    <div className="chat-grid">
      <Panel className="chat-peer-list"><span className="eyebrow">Active friends</span>{friends.length === 0 ? <div className="empty-state"><h3>No friends yet</h3><p>A discovered peer is not automatically a friend.</p><Button icon={UserPlus} onClick={() => navigate("/friends")}>Pair a friend</Button></div> : <ul className="chat-peer-items">{friends.map((friend) => <li key={friend.relationship_id}><button type="button" className={`chat-peer-item${selected?.peer_id === friend.peer_id ? " active" : ""}`} onClick={() => setSelected(friend)}><span className="friend-chat-avatar">{friend.node_name.slice(0, 1)}</span><span><strong>{friend.node_name}</strong><small>Explicitly paired</small></span></button></li>)}</ul>}</Panel>
      <Panel className="chat-conversation">{!selected ? <div className="empty-state"><h3>Select a friend</h3><p>Pair a friend before starting a private conversation.</p></div> : <>
        <div className="chat-conversation-head"><div><strong>{selected.node_name}</strong><div className="muted">Private relationship · {selected.endpoint}</div></div><Button icon={RefreshCw} onClick={() => void client.retryFriendMessages(selected.peer_id).then((result) => notify("info", `${result.delivered} queued message(s) delivered`))}>Retry queued</Button></div>
        <div className="chat-messages" ref={scrollRef}>{loading ? <LoadingPanel label="Loading private conversation" /> : messages.length === 0 ? <div className="empty-state"><h3>No messages yet</h3><p>Say hello. If they are offline, your message stays queued on this node.</p></div> : messages.map((row, index) => { const out = row.dir === "out"; const att = row.attachment; const url = att ? `${msgBase}/attachment/${encodeURIComponent(row.msg_id)}?peer_id=${encodeURIComponent(selected.peer_id)}` : ""; return <div key={row.msg_id || index} className={`chat-bubble${out ? " out" : " in"}`}>{row.text ? <span className="chat-text">{row.text}</span> : null}{att ? <a className="chat-attachment-chip" href={url} download={att.filename}><Paperclip size={13} />{att.filename}</a> : null}<span className="chat-meta">{formatTime(row.ts)} {out ? row.delivery_state === "queued" ? "· queued" : "✓" : ""}</span></div>; })}</div>
        <div className="chat-share-row"><BookOpen size={15} /><select aria-label="Choose content to share" value={shareId} onChange={(event) => setShareId(event.target.value)}><option value="">Share a content card from My Content…</option>{library.map((item) => <option key={item.library_id} value={item.library_id}>{item.title}</option>)}</select><Button disabled={!shareId} onClick={() => void share()}>Share card</Button></div>
        <div className="chat-composer"><input ref={inputRef} type="file" className="chat-file-input" onChange={(event) => setAttachment(event.target.files?.[0] ?? null)} /><Button icon={Paperclip} onClick={() => inputRef.current?.click()}>{attachment ? attachment.name : "Attach"}</Button><input className="chat-input" value={text} placeholder="Message your friend" onChange={(event) => setText(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} /><Button variant="primary" icon={SendHorizontal} disabled={sending} onClick={() => void send()}>Send</Button></div>
      </>}</Panel>
    </div>
    {cards.some((row) => row.dir !== "out") ? <Panel><span className="eyebrow">Shared with you · review before fetching</span><div className="friend-content-cards">{cards.filter((row) => row.dir !== "out").map((row) => <article key={row.card_id}><BookOpen size={18} /><div className="friend-card-body"><strong>{row.card.title}</strong><p>{row.card.summary}</p><small>From {friends.find((friend) => friend.peer_id === row.from)?.node_name ?? "a paired friend"} · Publisher {row.card.publisher_peer_id || "not declared"}</small><small>{row.card.source || "Unknown source"} · {row.card.filename || row.card.kind} · {formatBytes(row.card.size_bytes)}</small>{row.fetch_state === "fetched" ? <div className="friend-card-status verified"><CheckCircle2 size={14} /> SHA-256 verified · Saved to My Content <Button onClick={() => navigate("/library")}>Open</Button></div> : row.card.fetch_available ? <div className="friend-card-status"><ShieldCheck size={14} /> Body not downloaded <Button icon={Download} disabled={fetchingCardId === row.card_id} onClick={() => reviewAndFetch(row)}>Review &amp; get</Button></div> : <div className="friend-card-status">Metadata only · no downloadable body advertised</div>}</div></article>)}</div></Panel> : null}
  </div>;
}
