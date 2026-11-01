import { useUILanguage } from "../uiI18n";
import { useTranslation } from "react-i18next";
import { Lock, Paperclip, SendHorizontal } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useAppContext } from "../appContext";
import type { Peer } from "../domain/types";
import { usePersonal } from "../personal/model";
import { ConversationHead, EmptyDevices, LoadingConversation, MessageBubble, NoMessages, PeerList, dayKey, dayLabel, peerView, type ChatRecord } from "./components/ChatParts";
import "./friends-refresh.css";
import "./chat-refresh.css";

// The messages route base mirrors the live client's `${baseUrl}/messages`
// prefix. Derive it from the stream URL so packaged (Tauri) and dev builds
// resolve the same daemon base without re-implementing the precedence logic.
function attachmentBase(streamUrl: string): string {
  return streamUrl.replace(/\/stream$/, "");
}

async function fileToBase64(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

export default function Chat() {
  useUILanguage();
  const { t } = useTranslation();
  const { client, peers, notify } = useAppContext();
  const { devices, resolveName } = usePersonal();
  const conversationPeers = useMemo(() => peers.filter((peer) => !peer.isSelf), [peers]);
  const [selected, setSelected] = useState<Peer | null>(null);
  const [messages, setMessages] = useState<ChatRecord[]>([]);
  const [text, setText] = useState("");
  const [attachment, setAttachment] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [query, setQuery] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const selectedRef = useRef<Peer | null>(null);
  selectedRef.current = selected;

  const msgBase = attachmentBase(client.messagesStreamUrl());

  useEffect(() => {
    if (!selected && conversationPeers[0]) setSelected(conversationPeers[0]);
  }, [conversationPeers, selected]);

  // Load history when the open peer changes.
  useEffect(() => {
    if (!selected) {
      setMessages([]);
      return;
    }
    let alive = true;
    setLoading(true);
    void (async () => {
      try {
        const history = (await client.listMessages(selected.id)) as ChatRecord[];
        if (alive) setMessages(Array.isArray(history) ? history : []);
      } catch {
        if (alive) setMessages([]);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [client, selected]);

  // Subscribe once to the SSE stream; append records for the open peer.
  useEffect(() => {
    if (client.mode === "fixture") return;
    const source = new EventSource(client.messagesStreamUrl());
    source.onmessage = (event) => {
      let record: ChatRecord;
      try {
        record = JSON.parse(event.data) as ChatRecord;
      } catch {
        return;
      }
      const open = selectedRef.current;
      if (!open) return;
      // Incoming from the open peer, or our own echo addressed to them.
      if (record.from === open.id || record.to === open.id) {
        setMessages((current) => {
          if (record.msg_id && current.some((m) => m.msg_id === record.msg_id)) {
            return current.map((m) => (m.msg_id === record.msg_id ? record : m));
          }
          return [...current, record];
        });
      }
    };
    return () => source.close();
  }, [client]);

  // Keep the conversation scrolled to the newest message.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const send = async () => {
    if (!selected || sending) return;
    const trimmed = text.trim();
    if (!trimmed && !attachment) return;
    setSending(true);
    try {
      const payload: {
        peer_id: string;
        text?: string;
        attachment?: { filename: string; mime: string; bytes_b64: string };
      } = { peer_id: selected.id };
      if (trimmed) payload.text = trimmed;
      if (attachment) {
        payload.attachment = {
          filename: attachment.name,
          mime: attachment.type || "application/octet-stream",
          bytes_b64: await fileToBase64(attachment),
        };
      }
      const record = (await client.sendMessage(payload)) as ChatRecord;
      setMessages((current) =>
        record.msg_id && current.some((m) => m.msg_id === record.msg_id)
          ? current.map((m) => (m.msg_id === record.msg_id ? record : m))
          : [...current, record],
      );
      setText("");
      setAttachment(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
    } catch (error) {
      notify("danger", error instanceof Error ? error.message : t("personal.messagesMessageCouldNotBeSent"));
    } finally {
      setSending(false);
    }
  };

  const needle = query.trim().toLowerCase();
  const views = conversationPeers
    .map((peer) => ({ id: peer.id, peer, view: peerView(peer, devices, resolveName(peer.id, peer.name)) }))
    .filter(({ id, view }) => !needle || view.name.toLowerCase().includes(needle) || id.toLowerCase().includes(needle));
  const openView = selected ? peerView(selected, devices, resolveName(selected.id, selected.name)) : null;

  return (
    <div className="rf rd-chat">
      <header className="rq-head">
        <div className="rq-head-text">
          <h1>{t("personal.messagesMessages")}</h1>
          <p>{t("personal.messagesTalkDirectlyWithConnectedDevicesYourHistoryStaysOnThisDevice")}</p>
        </div>
        <span className="rq-seal"><Lock size={14} strokeWidth={1.8} aria-hidden="true" />{t("personal.messagesSealedForEachDeviceOnly")}</span>
      </header>

      {conversationPeers.length === 0 ? <EmptyDevices /> : (
        <section className="rq-chat rf-card">
          <PeerList views={views} selectedId={selected?.id} onSelect={(id) => setSelected(conversationPeers.find((peer) => peer.id === id) ?? null)} query={query} onQuery={setQuery} />

          <div className={`rq-conv${openView ? ` rf-h-${openView.hue}` : ""}`}>
            {!selected || !openView ? (
              <NoMessages title={t("personal.messagesSelectAConversation")} hint={t("personal.messagesPickADeviceOnTheLeftToOpenAConversation")} />
            ) : (
              <>
                <ConversationHead peerId={selected.id} view={openView} />
                <div className="rq-msgs" ref={scrollRef}>
                  {loading ? (
                    <LoadingConversation label={t("personal.messagesLoadingConversation")} />
                  ) : messages.length === 0 ? (
                    <NoMessages title={t("personal.messagesNoMessagesYet")} hint={t("personal.messagesSayHelloMessagesAreSealedForThisPeerOnly")} />
                  ) : (
                    messages.map((record, index) => {
                      const attUrl = record.attachment
                        ? `${msgBase}/attachment/${encodeURIComponent(record.msg_id)}?peer_id=${encodeURIComponent(selected.id)}`
                        : "";
                      const day = dayKey(record.ts);
                      const first = day !== "" && day !== dayKey(messages[index - 1]?.ts);
                      return (
                        <div key={record.msg_id || `${record.ts}-${index}`} className="rq-cgroup">
                          {first ? <div className="rq-day">{dayLabel(record.ts)}</div> : null}
                          <MessageBubble record={record} url={attUrl} peerName={openView.name} />
                        </div>
                      );
                    })
                  )}
                </div>
                <div className="rq-cdock">
                  <div className="rq-cbar">
                    <input ref={fileInputRef} type="file" className="rf-sr" tabIndex={-1} aria-label={t("personal.messagesAttachAFile")} onChange={(event) => setAttachment(event.target.files?.[0] ?? null)} />
                    <button type="button" className="rf-ib rq-cattach" title={t("personal.messagesAttachAFile")} onClick={() => fileInputRef.current?.click()}>
                      <Paperclip size={17} strokeWidth={1.7} aria-hidden="true" />
                      <span className="rf-sr">{attachment ? attachment.name : t("personal.messagesAttach")}</span>
                    </button>
                    {attachment ? <span className="rq-cchip"><Paperclip size={12} strokeWidth={1.8} aria-hidden="true" /><span className="rf-ell">{attachment.name}</span></span> : null}
                    <input
                      className="rq-cinput"
                      value={text}
                      aria-label={t("personal.messagesTypeAMessage")}
                      placeholder={t("personal.messagesTypeAMessage")}
                      onChange={(event) => setText(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                          event.preventDefault();
                          void send();
                        }
                      }}
                    />
                    <button type="button" className="rf-btn rf-btn-p" disabled={sending || (!text.trim() && !attachment)} onClick={() => void send()}>
                      <SendHorizontal size={14} strokeWidth={2} aria-hidden="true" />{t("personal.messagesSend")}
                    </button>
                  </div>
                  <small>{t("personal.messagesEnterToSendShiftEnterForANewLine")}</small>
                </div>
              </>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
