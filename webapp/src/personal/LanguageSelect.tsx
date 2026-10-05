import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Check, ChevronDown } from "lucide-react";
import { readLanguagePreference, setLanguagePreference, type LanguagePreference } from "../i18n";

const choices: LanguagePreference[] = ["system", "zh-CN", "en"];

export function LanguageSelect() {
  const { t } = useTranslation();
  const [preference, setPreference] = useState(readLanguagePreference);
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const labels: Record<LanguagePreference, string> = {
    system: t("settings.system"),
    "zh-CN": t("settings.chinese"),
    en: t("settings.english"),
  };

  useEffect(() => {
    const update = () => setPreference(readLanguagePreference());
    window.addEventListener("ryn-language-preference", update);
    window.addEventListener("storage", update);
    return () => {
      window.removeEventListener("ryn-language-preference", update);
      window.removeEventListener("storage", update);
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const choose = (value: LanguagePreference) => {
    setPreference(value);
    setLanguagePreference(value);
    setOpen(false);
    trigger.current?.focus();
  };

  return (
    <div className="pf-language-select" ref={root}>
      <button
        ref={trigger}
        type="button"
        className="pf-language-trigger"
        aria-label={t("settings.language")}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setOpen(true);
            const index = Math.max(0, choices.indexOf(preference) + (event.key === "ArrowDown" ? 1 : -1));
            window.requestAnimationFrame(() => optionRefs.current[Math.min(index, choices.length - 1)]?.focus());
          }
        }}
      >
        <span>{labels[preference]}</span>
        <ChevronDown size={16} aria-hidden="true" />
      </button>
      {open && (
        <div className="pf-language-menu" role="listbox" aria-label={t("settings.language")}>
          {choices.map((choice, index) => (
            <button
              key={choice}
              ref={(element) => { optionRefs.current[index] = element; }}
              type="button"
              role="option"
              aria-selected={preference === choice}
              className={preference === choice ? "selected" : ""}
              onClick={() => choose(choice)}
              onKeyDown={(event) => {
                if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                  event.preventDefault();
                  optionRefs.current[(index + (event.key === "ArrowDown" ? 1 : choices.length - 1)) % choices.length]?.focus();
                }
              }}
            >
              <span>{labels[choice]}</span>
              {preference === choice && <Check size={16} aria-hidden="true" />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
