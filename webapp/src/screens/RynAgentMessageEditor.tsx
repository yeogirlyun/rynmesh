import { useState, type ReactNode } from "react";
import { Pencil } from "lucide-react";
import { useTranslation } from "react-i18next";
import "./RynAgentMessageEditor.css";

interface Props {
  text: string;
  disabled: boolean;
  onSend(text: string): Promise<boolean>;
  children: ReactNode;
}

export default function RynAgentMessageEditor({ text, disabled, onSend, children }: Props) {
  const { t } = useTranslation("rynAgent");
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(text);
  const [sending, setSending] = useState(false);
  const submit = async () => {
    if (disabled || sending || !value.trim()) return;
    setSending(true);
    try { if (await onSend(value.trim())) setEditing(false); }
    finally { setSending(false); }
  };
  if (!editing) return <>{children}<div className="ra-message-actions"><button type="button" className="ra-edit-message" aria-label={t("editMessage")} title={t("editMessage")} disabled={disabled} onClick={() => { setValue(text); setEditing(true); }}><Pencil size={14} /></button></div></>;
  return <form className="ra-message-editor" onSubmit={event => { event.preventDefault(); void submit(); }}>
    <textarea autoFocus aria-label={t("editMessageText")} value={value} disabled={sending} rows={Math.max(3, Math.min(12, value.split("\n").length))} onChange={event => setValue(event.target.value)} onKeyDown={event => {
      if (event.nativeEvent.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape" && !sending) { event.preventDefault(); setEditing(false); }
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void submit(); }
    }} />
    <small>{t("editMessageHelp")}</small>
    <div><button type="button" disabled={sending} onClick={() => setEditing(false)}>{t("cancelEditMessage")}</button><button type="submit" className="ra-primary" disabled={disabled || sending || !value.trim()}>{t(sending ? "sendingMessage" : "saveAndSend")}</button></div>
  </form>;
}
