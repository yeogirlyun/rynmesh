import { ChevronRight } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { AgentMessage } from "../domain/rynAgent";
import ChatMarkdown from "./ChatMarkdown";

type TranscriptEntry = { kind: "message"; message: AgentMessage } | { kind: "progress"; messages: AgentMessage[] };

export function groupAgentMessages(messages: AgentMessage[]): TranscriptEntry[] {
  const entries: TranscriptEntry[] = [];
  for (const message of messages) {
    if (message.role === "assistant" && message.tool_calls?.length) {
      const previous = entries.at(-1);
      if (previous?.kind === "progress") previous.messages.push(message);
      else entries.push({ kind: "progress", messages: [message] });
    } else entries.push({ kind: "message", message });
  }
  return entries;
}

export default function RynAgentProgress({ messages, active }: { messages: AgentMessage[]; active: boolean }) {
  const { t } = useTranslation("rynAgent");
  const count = messages.reduce((total, message) => total + (message.tool_calls?.length || 0), 0);
  const latest = active ? messages.at(-1)?.content : null;
  return <div className="ra-process">
    {latest && <div className="ra-process-current"><ChatMarkdown>{latest}</ChatMarkdown></div>}
    <details className="ra-process-details">
      <summary><ChevronRight size={14} /><span>{t("processDetails")}</span><small>{t("processCount", { count })}</small></summary>
      <div className="ra-process-entries">{messages.map((message, index) => <div className="ra-process-entry" key={index}>
        {message.content && <ChatMarkdown>{message.content}</ChatMarkdown>}
        <div className="ra-process-tools">{message.tool_calls?.map(call => <span key={call.id}>{t(`tool_${call.function.name}`, { defaultValue: call.function.name })}</span>)}</div>
      </div>)}</div>
    </details>
  </div>;
}
