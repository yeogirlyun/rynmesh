import { tr, useUILanguage } from "../uiI18n";
import { Bot, CornerDownLeft, Server, User } from "lucide-react";
import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { useAppContext } from "../appContext";
import { Button, Chip, EmptyState, PageHeader, Panel } from "../components/ui";
import type { ConversationMessage } from "../domain/types";

const seed: ConversationMessage[] = [
  {
    id: "m1",
    role: "user",
    get text() { return tr("Search design references about urban gardens."); },
  },
  {
    id: "m2",
    role: "system",
    get text() { return tr("Routed via local Ryn node. Querying trusted peers for design and urban-gardens tags."); },
    operations: [
      { name: "discoverPeers", risk: "low", status: "done" },
      { name: "listContent(filters)", risk: "low", status: "done" },
      { name: "requestRecommendations(top 6)", risk: "low", status: "done" },
    ],
  },
  {
    id: "m3",
    role: "assistant",
    get text() { return tr("Two strong candidates from mira.studio are signed and safety-passed. A third from tomo-dataset is only metadata-reviewed, so I would fetch its preview before trusting the ranking."); },
    cites: ["cid_8f1a23b9c4", "cid_5012ff8801"],
    get suggests() { return [tr("Fetch previews for the top 3"), tr("Show only proven peers"), tr("More like this")]; },
  },
];

export default function SearchAsk() {
  useUILanguage();
  const { client, notify } = useAppContext();
  const [messages, setMessages] = useState<ConversationMessage[]>(client.mode === "fixture" ? seed : []);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);

  const send = async (event: FormEvent) => {
    event.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || sending) return;
    setText("");
    setSending(true);
    const userMessage: ConversationMessage = { id: crypto.randomUUID(), role: "user", text: trimmed };
    setMessages((current) => [...current, userMessage]);
    try {
      const response = await client.submitSearchAsk({ text: trimmed });
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "system", text: response.routing.text, operations: response.routing.operations },
        {
          id: crypto.randomUUID(),
          role: "assistant",
          text: response.assistant.text,
          cites: response.assistant.cites,
          suggests: response.assistant.suggests,
        },
      ]);
    } catch {
      notify("danger", tr("Local node could not complete the request"));
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="searchask-layout">
      <PageHeader
        eyebrow={tr("Search and Ask")}
        title={tr("Search & ask")}
        context={tr("Find content across connected devices and ask questions about it.")}
        actions={
          <>
            <Chip tone="ok">{tr("via local node")}</Chip>
            <Chip tone="muted">{tr("local model")}</Chip>
          </>
        }
      />

      <Panel className="conversation-panel">
        <div className="messages">
          {!messages.length && <EmptyState title={tr("Ask about your content")} body={tr("Search connected devices or ask for an explanation of a recommendation.")} />}
          {messages.map((message) => (
            <MessageBubble key={message.id} message={message} onSuggest={setText} />
          ))}
        </div>
        <form className="composer" onSubmit={send}>
          <textarea
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder={tr("Ask the node to search, rank, fetch previews, or explain recommendations...")}
          />
          <Button type="submit" variant="primary" icon={CornerDownLeft} disabled={sending || !text.trim()}>
            {sending ? tr("Routing") : tr("Send")}
          </Button>
        </form>
      </Panel>

      <Panel title={tr("Active policy")} className="policy-panel">
        <div className="policy-grid">
          <span>{tr("Network access")}</span>
          <Chip tone="ok">{tr("node mediated")}</Chip>
          <span>{tr("Discovery")}</span>
          <Chip tone="info">{tr("allowed")}</Chip>
          <span>{tr("Fetch on suggest")}</span>
          <Chip tone="warn">{tr("preview only")}</Chip>
          <span>{tr("Cloud model")}</span>
          <Chip tone="muted">{tr("disabled")}</Chip>
          <span>{tr("Safety policy")}</span>
          <Chip tone="info">{tr("standard")}</Chip>
        </div>
        <div className="try-list">
          {[tr("Find more like this."), tr("Show only proven peers."), tr("Fetch previews for the top 10."), tr("Explain why item 4 outranks item 7.")].map((suggestion) => (
            <button key={suggestion} type="button" onClick={() => setText(suggestion)}>
              {suggestion}
            </button>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function MessageBubble({
  message,
  onSuggest,
}: {
  message: ConversationMessage;
  onSuggest: (text: string) => void;
}) {
  useUILanguage();
  if (message.role === "system") {
    return (
      <div className="message message-system">
        <div className="message-title">
          <Server size={16} />
          {tr("Search activity")}
        </div>
        <p>{message.text}</p>
        <div className="operation-list">
          {message.operations?.map((operation) => (
            <span key={operation.name}>
              {operation.name}
              <Chip tone={operation.risk === "low" ? "info" : operation.risk === "medium" ? "warn" : "danger"}>
                {tr(operation.risk)}
              </Chip>
              <Chip tone={operation.status === "done" ? "ok" : "warn"}>{tr(operation.status)}</Chip>
            </span>
          ))}
        </div>
      </div>
    );
  }
  return (
    <div className={`message message-${message.role}`}>
      <div className="message-title">
        {message.role === "user" ? <User size={16} /> : <Bot size={16} />}
        {message.role === "user" ? tr("You") : tr("Assistant")}
      </div>
      <p>{message.text}</p>
      {message.cites?.length ? (
        <div className="cite-row">
          {message.cites.map((cite) => (
            <Link key={cite} to={`/items/${cite}`}>
              {cite}
            </Link>
          ))}
        </div>
      ) : null}
      {message.suggests?.length ? (
        <div className="suggestion-row">
          {message.suggests.map((suggestion) => (
            <button key={suggestion} type="button" onClick={() => onSuggest(suggestion)}>
              {suggestion}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
