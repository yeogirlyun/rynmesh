import { useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import "./RynAgentContextMenu.css";

export interface RynAgentContextMenuItem {
  id: string;
  label: string;
  description?: string;
  icon?: ReactNode;
  disabled?: boolean;
  danger?: boolean;
  onSelect: () => void;
}

export interface RynAgentContextMenuProps {
  anchor: { x: number; y: number };
  label: string;
  items: RynAgentContextMenuItem[];
  onClose: () => void;
}

/** Sidebar menus escape scroll containers and keep keyboard navigation local. */
export default function RynAgentContextMenu({ anchor, label, items, onClose }: RynAgentContextMenuProps) {
  const menu = useRef<HTMLDivElement>(null);
  const menuID = useId();
  const close = useRef(onClose);
  close.current = onClose;
  const [position, setPosition] = useState(anchor);

  useLayoutEffect(() => {
    const element = menu.current;
    if (!element) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const buttons = element.querySelectorAll<HTMLButtonElement>('button:not(:disabled)');
    (buttons[0] || element).focus();
    const dismiss = (event: PointerEvent) => {
      if (!element.contains(event.target as Node)) close.current();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      close.current();
    };
    const dismissOnScroll = (event: Event) => { if (!element.contains(event.target as Node)) close.current(); };
    document.addEventListener("pointerdown", dismiss, true);
    document.addEventListener("keydown", escape, true);
    document.addEventListener("scroll", dismissOnScroll, true);
    return () => {
      document.removeEventListener("pointerdown", dismiss, true);
      document.removeEventListener("keydown", escape, true);
      document.removeEventListener("scroll", dismissOnScroll, true);
      // Do not steal focus from a newly opened dialog or an outside control.
      if (previousFocus?.isConnected && (element.contains(document.activeElement) || document.activeElement === document.body)) previousFocus.focus();
    };
  }, []);

  useLayoutEffect(() => {
    const place = () => {
      const bounds = menu.current?.getBoundingClientRect();
      if (!bounds) return;
      setPosition({ x: Math.max(8, Math.min(anchor.x, window.innerWidth - bounds.width - 8)), y: Math.max(8, Math.min(anchor.y, window.innerHeight - bounds.height - 8)) });
    };
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [anchor.x, anchor.y, items.length]);

  return createPortal(<div ref={menu} className="ra-context-menu" role="menu" aria-label={label} tabIndex={-1} style={{ left: position.x, top: position.y }} onContextMenu={event => event.preventDefault()} onKeyDown={event => {
    const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'));
    const active = buttons.findIndex(button => button === document.activeElement);
    let next: number | undefined;
    if (event.key === "ArrowDown") next = (active + 1) % buttons.length;
    else if (event.key === "ArrowUp") next = (active - 1 + buttons.length) % buttons.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = buttons.length - 1;
    else if (event.key === "Tab") { event.preventDefault(); close.current(); }
    if (next !== undefined) { event.preventDefault(); buttons[next]?.focus(); }
  }}>{items.map(item => <button key={item.id} type="button" role="menuitem" aria-label={item.label} aria-describedby={item.description ? `${menuID}-${item.id}` : undefined} className={item.danger ? "ra-context-menu-danger" : undefined} title={item.description} disabled={item.disabled} onClick={() => { close.current(); item.onSelect(); }}>{item.icon && <span className="ra-context-menu-icon" aria-hidden="true">{item.icon}</span>}<span className="ra-context-menu-copy"><span>{item.label}</span>{item.description && <small id={`${menuID}-${item.id}`}>{item.description}</small>}</span></button>)}</div>, document.body);
}
