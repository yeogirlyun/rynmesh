import { FileText, Mail, Paperclip, RefreshCw, SendHorizontal } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import type { FriendMessage, FriendRecord } from "../../domain/friendTypes";
import { friendDeliveryExplanation, friendsApi } from "../../domain/friendsClient";
import type { Tone } from "../../personal/drop/FamilyParts";
import { tr, useUILanguage } from "../../uiI18n";
import { errorText } from "./friendLinks";
import { formatBytes, formatClock } from "./FriendsParts";
import "../friends-refresh.css";

const MAX_FILE = 5 * 1024 * 1024;
type SendBody = Parameters<typeof friendsApi.send>[1];
type DeliveryState = NonNullable<FriendMessage["delivery_state"]>;

const deliveryTone: Record<DeliveryState, Tone> = { queued: "off", sending: "sky", mailbox: "warn", delivered: "ok", failed: "bad", expired: "off" };
function deliveryLabel(state: DeliveryState): string {
  switch (state) {
    case "sending": return tr("Sending");
    case "mailbox": return tr("In encrypted mailbox · waiting for confirmation");
    case "delivered": return tr("Delivered · confirmed by your friend");
    case "failed": return tr("Could not confirm delivery · retry available");
    case "expired": return tr("Expired · delivery unconfirmed");
    default: return tr("Waiting to send");
  }
}

async function encodeAttachment(file: File): Promise<NonNullable<SendBody["attachment"]>> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  return { filename: file.name, mime: file.type || "application/octet-stream", data_base64: btoa(binary) };
}

export default function FriendConversation({ friend, focusMessage }: { friend: FriendRecord; focusMessage?: string | null }) {
  useUILanguage();
  const [messages, setMessages] = useState<FriendMessage[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [pending, setPending] = useState<SendBody | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const alive = useRef(true);
  useEffect(() => {
    if (!loaded || !focusMessage) return;
    const element = document.getElementById(`friend-message-${focusMessage}`);
    element?.focus({ preventScroll: true }); element?.scrollIntoView?.({ block: "center" });
  }, [loaded, focusMessage]);
  const refresh = useCallback(async () => {
    const result = await friendsApi.history(friend.peer_id);
    if (alive.current) { setMessages(result.messages); setLoaded(true); setLoadError(""); }
  }, [friend.peer_id]);
  useEffect(() => {
    alive.current = true;
    let running = false;
    const poll = async () => {
      if (running) return;
      running = true;
      try { await refresh(); }
      catch { if (alive.current) setLoadError(tr("Could not refresh messages. Retry when the local node is available.")); }
      finally { running = false; }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 5000);
    return () => { alive.current = false; window.clearInterval(timer); };
  }, [refresh]);
  const act = async (operation: () => Promise<void>) => {
    setBusy(true); setError("");
    try { await operation(); }
    catch (cause) { if (alive.current) setError(errorText(cause, tr("Could not confirm the operation. Retry."))); }
    finally { if (alive.current) setBusy(false); }
  };
  const send = async () => {
    const request = pending ?? { message_id: crypto.randomUUID().replaceAll("-", ""), text,
      ...(file ? { attachment: await encodeAttachment(file) } : {}) };
    setPending(request);
    await friendsApi.send(friend.peer_id, request);
    if (!alive.current) return;
    setPending(null); setText(""); setFile(null);
    if (fileInput.current) fileInput.current.value = "";
    await refresh();
  };
  const download = async (message: FriendMessage) => {
    const response = await fetch(friendsApi.attachmentUrl(friend.peer_id, message.msg_id), { credentials: "include" });
    if (!response.ok) throw new Error(tr("Could not download this attachment. Access may have been removed."));
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = message.attachment?.filename ?? "attachment";
    anchor.click(); window.setTimeout(() => URL.revokeObjectURL(url), 10000);
  };

  const outgoing = messages.filter((message) => message.dir === "out");
  const count = (state: DeliveryState) => outgoing.filter((message) => message.delivery_state === state).length;
  const failed = count("failed"), mailbox = count("mailbox"), queued = count("queued");
  const canRetry = outgoing.some((message) => ["queued", "mailbox", "failed"].includes(message.delivery_state ?? ""));

  return <div className="rq-convo">
    {canRetry ? <div className="rq-banner rq-banner-warn">
      <Mail size={16} strokeWidth={1.8} aria-hidden="true" />
      <span className="rq-banner-text">
        {failed ? <span>{tr("{{count}} messages could not be confirmed. Retry keeps the same message identity and content.", { count: failed })}</span> : null}
        {mailbox ? <span>{tr("{{count}} messages are in the encrypted mailbox, waiting for {{name}} to confirm.", { count: mailbox, name: friend.node_name })}</span> : null}
        {queued ? <span>{tr("{{count}} messages are waiting to send.", { count: queued })}</span> : null}
      </span>
      <button type="button" className="rf-btn rf-btn-sm" disabled={busy} onClick={() => void act(async () => { await friendsApi.retry(friend.peer_id); await refresh(); })}>{tr("Retry next pending message")}</button>
    </div> : null}
    {error || loadError ? <p className="rq-banner rq-banner-bad" role="alert">{error || loadError}</p> : null}
    <div className="rq-thread">
      <button type="button" className="rf-ib rf-ib-sm rq-thread-refresh" aria-label={tr("Refresh messages")} title={tr("Refresh messages")} disabled={busy} onClick={() => void act(refresh)}><RefreshCw size={14} strokeWidth={1.7} aria-hidden="true" /></button>
      {!loaded ? <div className="rq-skel" aria-hidden="true"><i /><i /><i /></div> : null}
      {!loaded ? <p className="rq-loading">{tr("Loading messages…")}</p>
        : messages.length === 0 ? <div className="rq-none">
          <Mail size={30} strokeWidth={1.4} aria-hidden="true" />
          <b>{tr("Send your first message.")}</b>
          <span>{tr("Messages, small attachments and content cards are allowed. AI access stays off.")}</span>
        </div> : <ol aria-label={tr("Conversation")}>
          {messages.map((message) => {
            const incoming = message.dir === "in";
            const state: DeliveryState = message.delivery_state ?? "queued";
            const tone: Tone = incoming ? "off" : deliveryTone[state];
            const explanation = incoming ? "" : friendDeliveryExplanation(message);
            const attachment = message.attachment;
            return <li key={message.msg_id} id={`friend-message-${message.msg_id}`} tabIndex={-1} data-dir={message.dir} data-focus={message.msg_id === focusMessage}>
              <span className="rf-sr">{incoming ? friend.node_name : tr("You")}</span>
              {message.text ? <div className="rq-bubble">{message.text}</div> : null}
              {attachment ? <button type="button" className="rq-file" disabled={busy}
                aria-label={tr("Save attachment: {{name}} ({{size}} bytes)", { name: attachment.filename, size: attachment.size ?? 0 })}
                onClick={() => void act(() => download(message))}>
                <span className="rq-file-ico rf-h-rose" aria-hidden="true"><FileText size={13} strokeWidth={2} /></span>
                <span className="rf-ell">{attachment.filename}</span>
                <small>{formatBytes(attachment.size ?? 0)}</small>
                <b>{tr("Save")}</b>
              </button> : null}
              <small className={`rq-meta rf-t-${tone}`}><i aria-hidden="true" /><span>{incoming ? tr("Received") : deliveryLabel(state)}</span>{message.ts ? <span>{` · ${formatClock(message.ts)}`}</span> : null}</small>
              {explanation ? <p role="status" className="rq-explain">{tr(explanation)}</p> : null}
            </li>;
          })}
        </ol>}
    </div>
    <div className="rq-composer">
      <textarea aria-label={tr("Message")} rows={2} maxLength={32000} placeholder={tr("Write to {{name}}…", { name: friend.node_name })} value={text} disabled={busy || !!pending} onChange={(event) => setText(event.target.value)} />
      {file ? <p className="rq-attached"><Paperclip size={13} strokeWidth={1.8} aria-hidden="true" /><span className="rf-ell">{file.name}</span><small>{formatBytes(file.size)}</small></p> : null}
      {pending ? <p className="rq-pending">{tr("The send result is unconfirmed. Retry keeps the same message identity and content.")}</p> : null}
      <div className="rq-composer-bar">
        <input ref={fileInput} aria-label={tr("Attachment")} className="rf-sr" type="file" tabIndex={-1} disabled={busy || !!pending} onChange={(event) => {
          const next = event.target.files?.[0] ?? null;
          if (next && next.size > MAX_FILE) { setError(tr("Attachment exceeds 5 MiB. Choose a smaller file.")); setFile(null); event.target.value = ""; }
          else { setFile(next); setError(""); }
        }} />
        <button type="button" className="rf-ib rq-attach" disabled={busy || !!pending} onClick={() => fileInput.current?.click()}><Paperclip size={15} strokeWidth={1.7} aria-hidden="true" />{tr("Attach")}</button>
        <small title={tr("Attachments: up to 5 MiB for direct delivery. The mailbox accepts envelopes up to 64 KiB including encryption; larger messages wait for a direct connection.")}>{tr("Up to 5 MiB direct · mailbox envelopes up to 64 KiB")}</small>
        <button type="button" className="rf-btn rf-btn-p rf-btn-sm" disabled={busy || !loaded || (!pending && !text.trim() && !file)} onClick={() => void act(send)}><SendHorizontal size={14} strokeWidth={2} aria-hidden="true" />{busy ? tr("Working…") : pending ? tr("Retry this send") : tr("Send message")}</button>
      </div>
    </div>
  </div>;
}
