import { useId, useState, type KeyboardEvent, type ReactNode } from "react";
import { ChevronDown, Check } from "lucide-react";
import { tr } from "../uiI18n";

// Presentational pieces for the Service setup page (tabs, option cards, segmented control, disclosure, status pill). Styles live in
// setup-refresh.css under `.rd-setup`; buttons, pills and tiles come from personal/drop/family.css through `.rf`.

export type SetupTab = "local" | "orders" | "video";
export type PillTone = "ok" | "off" | "warn" | "self" | "sky" | "bad";

export function SvPill({ tone, children, dot = true, line, className = "" }: { tone: PillTone; children: ReactNode; dot?: boolean; line?: boolean; className?: string }) {
  return <span className={`rf-pill rf-t-${tone}${line ? " rf-pill-line" : ""} ${className}`.trim()}>{dot ? <i aria-hidden="true" /> : null}<span className="sv-cap">{children}</span></span>;
}

/** Tab strip; every pane stays mounted (hidden by CSS `data-on`) so state and polling in an inactive pane keep running. */
export function SetupTabs({ tab, tabs, onChange }: { tab: SetupTab; tabs: ReadonlyArray<readonly [SetupTab, string]>; onChange: (next: SetupTab) => void }) {
  const move = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const key = event.key;
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(key)) return;
    event.preventDefault();
    const next = key === "Home" ? 0 : key === "End" ? tabs.length - 1 : (index + (key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    onChange(tabs[next][0]);
    (event.currentTarget.parentElement?.children[next] as HTMLElement | undefined)?.focus();
  };
  return (
    <div className="sv-tabs" role="tablist" aria-label={tr("Service setup sections")}>
      {tabs.map(([id, label], index) => (
        <button key={id} type="button" role="tab" id={`sv-tab-${id}`} aria-selected={tab === id} aria-controls={`sv-pane-${id}`} tabIndex={tab === id ? 0 : -1} onClick={() => onChange(id)} onKeyDown={(event) => move(event, index)}>{label}</button>
      ))}
    </div>
  );
}

export function TabPane({ id, on, children }: { id: SetupTab; on: boolean; children: ReactNode }) {
  return <div className="sv-pane" role="tabpanel" id={`sv-pane-${id}`} aria-labelledby={`sv-tab-${id}`} data-on={on}>{children}</div>;
}

/** Card choices for a single-choice setting (setup mode). The matching `<select>` stays in the page for assistive tech. */
export function OptionCards<T extends string>({ label, value, options, onChange }: { label: string; value: T | ""; options: ReadonlyArray<{ id: T; title: string; desc: string }>; onChange: (next: T) => void }) {
  return (
    <div className="sv-opts" role="group" aria-label={label}>
      {options.map((option) => (
        <button key={option.id} type="button" className="sv-opt" aria-pressed={value === option.id} onClick={() => onChange(option.id)}>
          <span className="sv-opt-head"><b>{option.title}</b>{value === option.id ? <Check size={16} strokeWidth={2.4} aria-hidden="true" /> : null}</span>
          <small>{option.desc}</small>
        </button>
      ))}
    </div>
  );
}

/** Segmented single-choice control. */
export function SegChoice<T extends string>({ label, value, options, onChange }: { label: string; value: T; options: ReadonlyArray<{ id: T; label: string }>; onChange: (next: T) => void }) {
  return (
    <div className="rf-seg sv-segfill" role="group" aria-label={label}>
      {options.map((option) => <button key={option.id} type="button" aria-pressed={value === option.id} onClick={() => onChange(option.id)}>{option.label}</button>)}
    </div>
  );
}

/** A collapsible section whose body stays mounted while closed (hidden by CSS), so its controls remain reachable. */
export function Collapse({ title, icon, tone = "bad", children }: { title: string; icon?: ReactNode; tone?: "bad" | "plain"; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <div className={`sv-collapse sv-collapse-${tone}`} data-open={open}>
      <button type="button" className="sv-collapse-head" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
        {icon}<span>{title}</span><ChevronDown size={15} aria-hidden="true" />
      </button>
      <div id={id} className="sv-collapse-body">{children}</div>
    </div>
  );
}
