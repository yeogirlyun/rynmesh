import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { isTauri } from "@tauri-apps/api/core";
import { useNavigate } from "react-router-dom";
import { subscribeInviteLinks } from "../domain/inviteDeepLinks";

const InviteContext = createContext({ pending: "", clear: () => {} });
export const useInviteDeepLink = () => useContext(InviteContext);

export default function InviteDeepLinks({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;
  const [pending, setPending] = useState("");
  const pendingRef = useRef("");
  const [notice, setNotice] = useState("");
  useEffect(() => {
    if (!isTauri()) return;
    let active = true;
    let stop: (() => void) | undefined;
    void import("@tauri-apps/plugin-deep-link").then((source) => {
      if (!active) return;
      stop = subscribeInviteLinks(source, (invite) => {
        if (pendingRef.current && pendingRef.current !== invite) {
          setNotice("Another invitation arrived. Finish or dismiss the pending invitation, then open the other link again.");
          return;
        }
        pendingRef.current = invite;
        setPending(invite);
        navigateRef.current("/friends");
      }, () => setNotice("Could not open this invitation. Open Friends and paste the complete invitation to review it."));
    }).catch(() => { if (active) setNotice("Could not open this invitation. Paste it in Friends instead."); });
    return () => { active = false; stop?.(); };
  }, []);
  return <InviteContext.Provider value={{ pending, clear: () => { pendingRef.current = ""; setPending(""); setNotice(""); } }}>
    {notice ? <div role="status">{notice} <button onClick={() => setNotice("")}>Dismiss notice</button></div> : null}
    {children}
  </InviteContext.Provider>;
}
