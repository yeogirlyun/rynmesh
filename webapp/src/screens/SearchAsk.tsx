import { Bot, CornerDownLeft, Server, User } from "lucide-react";
import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { useAppContext } from "../appContext";
import { Button, Chip, PageHeader, Panel } from "../components/ui";
import type { ConversationMessage } from "../domain/types";

const seed: ConversationMessage[] = [
  {
    id: "m1",
    role: "user",
    text: "Search design references about urban gardens.",
  },
  {
    id: "m2",
    role: "system",
    text: "Routed via local Ryn node. Querying trusted peers for design and urban-gardens tags.",
    operations: [
      { name: "discoverPeers", risk: "low", status: "done" },
      { name: "listContent(filters)", risk: "low", status: "done" },
      { name: "requestRecommendations(top 6)", risk: "low", status: "done" },
    ],
  },
  {
    id: "m3",
    role: "assistant",
    text: "Two strong candidates from mira.studio are signed and safety-passed. A third from tomo-dataset is only metadata-reviewed, so I would fetch its preview before trusting the ranking.",
    cites: ["cid_8f1a23b9c4", "cid_5012ff8801"],
    suggests: ["Fetch previews for the top 3", "Show only proven peers", "More like this"],
  },
];

export default function SearchAsk() {
  const { client, notify } = useAppContext();
  const [messages, setMessages] = useState<ConversationMessage[]>(seed);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);

  const send = async (event: FormEvent) => {
    event.preventDefault();
    const trimmed = text.trim();
    if (!trimmed) return;
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
      notify("danger", "Local node could not complete the request");
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="searchask-layout">
      <PageHeader
        eyebrow="Search and Ask"
        title="Steer the node"
        context="Conversational review where routing is visible. The AI curator recommends; the local node acts."
        actions={
          <>
            <Chip tone="ok">via local node</Chip>
            <Chip tone="muted">local model</Chip>
          </>
        }
      />

      <Panel className="conversation-panel">
        <div className="messages">
          {messages.map((message) => (
            <MessageBubble key={message.id} message={message} onSuggest={setText} />
          ))}
        </div>
        <form className="composer" onSubmit={send}>
          <textarea
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder="Ask the node to search, rank, fetch previews, or explain recommendations..."
          />
          <Button type="submit" variant="primary" icon={CornerDownLeft} disabled={sending}>
            {sending ? "Routing" : "Send"}
          </Button>
        </form>
      </Panel>

      <Panel title="Active policy" className="policy-panel">
        <div className="policy-grid">
          <span>Network access</span>
          <Chip tone="ok">node mediated</Chip>
          <span>Discovery</span>
          <Chip tone="info">allowed</Chip>
          <span>Fetch on suggest</span>
          <Chip tone="warn">preview only</Chip>
          <span>Cloud model</span>
          <Chip tone="muted">disabled</Chip>
          <span>Safety policy</span>
          <Chip tone="info">standard</Chip>
        </div>
        <div className="try-list">
          {["Find more like this.", "Show only proven peers.", "Fetch previews for the top 10.", "Explain why item 4 outranks item 7."].map((suggestion) => (
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
  if (message.role === "system") {
    return (
      <div className="message message-system">
        <div className="message-title">
          <Server size={16} />
          Local Ryn node routing
        </div>
        <p>{message.text}</p>
        <div className="operation-list">
          {message.operations?.map((operation) => (
            <span key={operation.name}>
              {operation.name}
              <Chip tone={operation.risk === "low" ? "info" : operation.risk === "medium" ? "warn" : "danger"}>
                {operation.risk}
              </Chip>
              <Chip tone={operation.status === "done" ? "ok" : "warn"}>{operation.status}</Chip>
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
        {message.role === "user" ? "You - sent to local node" : "AI curator"}
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
