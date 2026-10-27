import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { ArrowRightLeft, FolderUp, MessageCircle, Radio, Send } from "lucide-react";
import type { MapAnchor } from "../DeviceTopology";
import type { DropDevice } from "./dropModel";
import "./family.css";
import { canSendTo } from "./dropModel";

export type DeviceMenuAction = "send_files" | "send_folder" | "message" | "history" | "details";

export const NARROW_QUERY = "(max-width: 736px)";
export const isNarrow = () => typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(NARROW_QUERY).matches;

/**
 * Device actions menu for graph nodes, offline rows and conversation items.
 * Opens at the pointer (or the invoking element), stays inside the window,
 * closes on outside click, Esc, scroll, resize or action, and returns focus
 * to the invoker. Narrow screens get a bottom action sheet.
 */
export function DeviceActionsMenu({ device, anchor, onAction, onClose }: {
  device: DropDevice;
  anchor: MapAnchor;
  onAction: (action: DeviceMenuAction, device: DropDevice) => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const menu = useRef<HTMLDivElement>(null);
  const invoker = useRef<HTMLElement | null>(anchor instanceof HTMLElement ? anchor : (document.activeElement as HTMLElement | null));
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);
  const [sheet] = useState(isNarrow);
  const sendable = canSendTo(device);
  const messageable = !device.self && Boolean(device.meta) && device.relationship !== "other";
  const items: { action: DeviceMenuAction; label: string; icon: typeof Send; disabled?: boolean; separator?: boolean }[] = [
    { action: "send_files", label: t("drop.sendFile"), icon: Send, disabled: !sendable },
    { action: "send_folder", label: t("drop.sendFolder"), icon: FolderUp, disabled: !sendable },
    { action: "message", label: t("drop.sendMessage"), icon: MessageCircle, disabled: !messageable },
    { action: "history", label: t("drop.transferHistory"), icon: ArrowRightLeft, separator: true },
    { action: "details", label: t("drop.connectionDetails"), icon: Radio },
  ];

  const close = (restore = true) => {
    if (restore && invoker.current?.isConnected) invoker.current.focus({ preventScroll: true });
    onClose();
  };
  const closeRef = useRef(close);
  closeRef.current = close;

  useLayoutEffect(() => {
    const element = menu.current;
    if (!element || sheet) return;
    const margin = 8;
    const rect = element.getBoundingClientRect();
    const point = anchor instanceof HTMLElement
      ? (() => { const r = anchor.getBoundingClientRect(); return { x: r.left, y: r.bottom + 4, flipY: r.top - 4 }; })()
      : { x: anchor.x, y: anchor.y, flipY: anchor.y };
    let left = point.x;
    let top = point.y;
    if (left + rect.width > window.innerWidth - margin) left = Math.max(margin, (anchor instanceof HTMLElement ? anchor.getBoundingClientRect().right : point.x) - rect.width);
    if (left + rect.width > window.innerWidth - margin) left = window.innerWidth - margin - rect.width;
    if (top + rect.height > window.innerHeight - margin) top = point.flipY - rect.height;
    left = Math.max(margin, left);
    top = Math.max(margin, Math.min(top, window.innerHeight - margin - rect.height));
    setPosition({ left, top });
  }, [anchor, sheet]);

  useEffect(() => {
    const first = menu.current?.querySelector<HTMLButtonElement>("[role=menuitem]:not(:disabled)");
    first?.focus({ preventScroll: true });
    const outside = (event: PointerEvent) => {
      if (menu.current?.contains(event.target as Node)) return;
      if (event.button !== 0 && event.pointerType === "mouse") return;
      closeRef.current(false);
    };
    const dismiss = () => closeRef.current(false);
    const scroll = (event: Event) => { if (!menu.current?.contains(event.target as Node)) dismiss(); };
    const key = (event: globalThis.KeyboardEvent) => { if (event.key === "Escape") { event.preventDefault(); closeRef.current(true); } };
    document.addEventListener("pointerdown", outside, true);
    window.addEventListener("scroll", scroll, true);
    window.addEventListener("resize", dismiss);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("pointerdown", outside, true);
      window.removeEventListener("scroll", scroll, true);
      window.removeEventListener("resize", dismiss);
      document.removeEventListener("keydown", key);
    };
  }, []);

  const navigate = (event: KeyboardEvent<HTMLDivElement>) => {
    const buttons = Array.from(menu.current?.querySelectorAll<HTMLButtonElement>("[role=menuitem]:not(:disabled)") || []);
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
    let next = -1;
    if (event.key === "ArrowDown") next = (index + 1) % buttons.length;
    else if (event.key === "ArrowUp") next = (index - 1 + buttons.length) % buttons.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = buttons.length - 1;
    else if (event.key === "Tab") { event.preventDefault(); closeRef.current(true); return; }
    if (next >= 0) { event.preventDefault(); buttons[next]?.focus({ preventScroll: true }); }
  };

  const content = <div className={`pf-device-menu-layer rf rf-menu-layer${sheet ? " sheet" : ""}`}>
    {sheet && <button type="button" className="pf-device-menu-backdrop rf-menu-backdrop" aria-label={t("drop.close")} tabIndex={-1} onClick={() => close(true)} />}
    <div ref={menu} role="menu" aria-label={t("drop.deviceActions", { name: device.name })}
      className={`pf-device-menu rf-menu${sheet ? " sheet" : ""}`} onKeyDown={navigate}
      style={sheet ? undefined : position ? { left: position.left, top: position.top } : { left: -9999, top: -9999 }}>
      <div className="pf-device-menu-title rf-menu-title" aria-hidden="true">{device.name}</div>
      {items.map(item => <div key={item.action} role="none">
        {item.separator && <hr />}
        <button type="button" role="menuitem" disabled={item.disabled}
          onClick={() => { close(true); onAction(item.action, device); }}>
          <item.icon size={16} strokeWidth={1.7} aria-hidden="true" />{item.label}
        </button>
      </div>)}
    </div>
  </div>;
  return createPortal(content, document.body);
}

/** Small state holder shared by every surface that opens the device menu. */
export function useDeviceMenu() {
  const [state, setState] = useState<{ device: DropDevice; anchor: MapAnchor } | null>(null);
  return {
    menu: state,
    open: (device: DropDevice, anchor: MapAnchor) => setState({ device, anchor }),
    close: () => setState(null),
  };
}
