import { useTranslation } from "react-i18next";
import { CircleAlert } from "lucide-react";

/** Render only messages tagged by the runtime, never infer authority from text. */
export default function RynAgentVerification({ content }: { content: string | null }) {
  const { t } = useTranslation("rynAgent");
  return <section className="ra-verification-feedback" aria-label={t("verificationFeedback")}>
    <strong><CircleAlert size={16} aria-hidden="true" />{t("verificationFeedback")}</strong>
    <p>{t("verificationFeedbackHelp")}</p>
    {content && <details className="ra-event-evidence"><summary>{t("verificationFeedbackDetails")}</summary><pre>{content}</pre></details>}
  </section>;
}
