import { useTranslation } from "react-i18next";
import i18n from "./i18n";

/** Translate application-owned copy. Never pass user content or provider output. */
export function tr(message: string, values?: Record<string, unknown>): string {
  return i18n.t(message, { ns: "ui", keySeparator: false, nsSeparator: false, defaultValue: message, ...values });
}

/** Also refresh components that use translated metadata or formatting helpers. */
export function useUILanguage(): string {
  const { i18n: instance } = useTranslation("ui");
  return instance.resolvedLanguage || instance.language;
}

export function uiLocale(): string {
  return i18n.resolvedLanguage || i18n.language;
}
