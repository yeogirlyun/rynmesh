import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LockKeyhole } from "lucide-react";
import { Modal } from "../personal/components";
import type { AgentTask } from "../domain/rynAgent";
import RynAgentCredential from "./RynAgentCredential";

export default function RynAgentInputCard({ task, busy, onSaved, onChangeMethod }: {
  task: AgentTask; busy: boolean; onSaved: (task: AgentTask) => void; onChangeMethod: () => void;
}) {
  const { t } = useTranslation("rynAgent");
  const [open, setOpen] = useState(false);
  return <>
    <section className="ra-input-card" aria-label={t("credentialCard")}>
      <LockKeyhole size={19} />
      <div><strong>{t("credentialCard")}</strong><p>{t("credentialCardHelp")}</p>
        <div className="ra-input-card-actions"><button className="ra-primary" onClick={() => setOpen(true)}>{t("openCredential")}</button><button className="ra-link" disabled={busy} onClick={onChangeMethod}>{t("changeSourceMethod")}</button></div>
      </div>
    </section>
    {open && <Modal title={t("openCredential")} onClose={() => setOpen(false)}>
      <div className="ra-credential-dialog"><RynAgentCredential task={task} busy={busy} onSaved={onSaved} /></div>
    </Modal>}
  </>;
}
