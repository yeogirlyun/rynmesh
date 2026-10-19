import { File } from "lucide-react";
import type { AgentMessageAttachment } from "../domain/agentAttachments";
import "./RynAgentComposer.css";

/** Render the server's persisted attachment metadata alongside the owner text. */
export default function AgentMessageAttachments({ attachments }: { attachments?: AgentMessageAttachment[] }) {
  if (!attachments?.length) return null;
  return <div className="ra-message-attachments">{attachments.map(file => <div className="ra-message-attachment" key={file.id} title={file.path || file.name}>
    {file.image_url ? <img src={file.image_url} alt={file.name} /> : <File size={22} />}<span>{file.name}</span>
  </div>)}</div>;
}
