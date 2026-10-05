import { tr, useUILanguage } from "../uiI18n";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Check, ChevronDown } from "lucide-react";
import styles from "./PrivateAIChat.module.css";

export type WorkspaceOption = { value: string; label: string; icon?: ReactNode; detail?: string };

export default function WorkspaceSelect({ label, caption, value, options, disabled, icon, grow, placeholder, onChange }: {
  label: string; caption: string; value: string; options: WorkspaceOption[];
  disabled?: boolean; icon?: ReactNode; grow?: boolean; placeholder?: string; onChange: (value: string) => void;
}) {
  useUILanguage();
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const choices = useRef<(HTMLButtonElement | null)[]>([]);
  const id = useId();
  const selected = options.find(option => option.value === value);
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);
  useEffect(() => {
    if (!open) return;
    choices.current[Math.max(0, options.findIndex(option => option.value === value))]?.focus();
    const outside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  return <div ref={root} className={`${styles.picker} ${grow ? styles.devicePicker : ""}`}
    onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setOpen(false); }}
    onKeyDown={event => {
      if (event.key === "Escape") { event.preventDefault(); setOpen(false); trigger.current?.focus(); }
    }}>
    <button ref={trigger} type="button" className={styles.pickerTrigger} aria-label={label}
      aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? id : undefined} disabled={disabled}
      onClick={() => setOpen(!open)} onKeyDown={event => {
        if (["ArrowDown", "ArrowUp"].includes(event.key)) { event.preventDefault(); setOpen(true); }
      }}>
      {icon && <span className={styles.pickerIcon}>{icon}</span>}
      <span className={styles.pickerText}><span className={styles.fieldCaption}>{caption}</span><span>{selected?.label || placeholder || tr("请选择")}</span></span>
      <ChevronDown size={15} className={styles.pickerChevron} aria-hidden="true" />
    </button>
    {open && <div id={id} className={styles.pickerMenu} role="listbox" aria-label={label}>
      {options.map((option, index) => <button key={option.value} ref={element => { choices.current[index] = element; }}
        type="button" role="option" aria-selected={option.value === value} tabIndex={-1}
        className={styles.pickerOption} onClick={() => { onChange(option.value); setOpen(false); trigger.current?.focus(); }}
        onKeyDown={event => {
          let next = index;
          if (event.key === "ArrowDown") next = (index + 1) % options.length;
          else if (event.key === "ArrowUp") next = (index + options.length - 1) % options.length;
          else if (event.key === "Home") next = 0;
          else if (event.key === "End") next = options.length - 1;
          else return;
          event.preventDefault(); choices.current[next]?.focus();
        }}>
        {option.icon && <span className={styles.optionIcon}>{option.icon}</span>}
        <span className={styles.optionText}>{option.label}{option.detail && <small>{option.detail}</small>}</span>
        {option.value === value && <Check size={15} aria-hidden="true" />}
      </button>)}
    </div>}
  </div>;
}
