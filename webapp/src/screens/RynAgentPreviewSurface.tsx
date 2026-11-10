import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

export const AgentPreviewHostContext = createContext<{
  host: HTMLElement | null; onOpen: () => void; hidden: boolean;
} | null>(null);

/** Presentation only; loading, versions and access checks stay in TaskArtifacts. */
export default function RynAgentPreviewSurface({ children, onClose, label }: {
  children: ReactNode; onClose: () => void; label: string;
}) {
  const context = useContext(AgentPreviewHostContext);
  const ref = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  const [narrow, setNarrow] = useState(() => typeof matchMedia === "function" && matchMedia("(max-width: 1100px)").matches);
  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const query = matchMedia("(max-width: 1100px)");
    const workspace = context?.host?.closest<HTMLElement>(".ra-workspace");
    const update = () => setNarrow(workspace ? workspace.clientWidth <= 1100 : query.matches);
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(update) : null;
    if (workspace) observer?.observe(workspace);
    update();
    query.addEventListener("change", update);
    return () => { observer?.disconnect(); query.removeEventListener("change", update); };
  }, [context?.host]);
  const onOpen = context?.onOpen;
  useEffect(() => { onOpen?.(); }, [onOpen]);
  const host = context?.host;
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const panel = ref.current;
    panel?.focus();
    return () => {
      if (previous?.isConnected && (panel?.contains(document.activeElement) || document.activeElement === document.body)) previous.focus();
    };
  }, [host]);
  const content = <div className={`ra-artifact-preview${host ? " ra-docked-preview" : ""}`} ref={ref} tabIndex={-1}
    role={narrow ? "dialog" : "region"} aria-modal={narrow || undefined} aria-label={label}
    onKeyDown={event => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close.current(); }
      if (!narrow || event.key !== "Tab") return;
      const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]')).filter(item => item.getClientRects().length > 0);
      const first = controls[0], last = controls.at(-1);
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === ref.current)) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === ref.current)) { event.preventDefault(); first.focus(); }
    }}>{children}</div>;
  return host ? createPortal(content, host) : content;
}
