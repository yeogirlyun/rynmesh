import { useEffect, useState } from "react";
import { friendsApi, type FriendDiagnostics } from "../../domain/friendsClient";
import type { FriendRecord } from "../../domain/friendTypes";
import { tr } from "../../uiI18n";
import { errorText } from "./friendLinks";

/** Connection-check state for a set of friends. Lifted into a hook so the page header and the Connection tab share one result. */
export function useFriendDiagnostics(friends: FriendRecord[], options: { loadOnMount?: boolean } = {}) {
  const [selected, setSelected] = useState<string[]>([]);
  const [result, setResult] = useState<FriendDiagnostics | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const ids = friends.map((friend) => friend.relationship_id).join(",");
  useEffect(() => { setSelected(ids ? ids.split(",").slice(0, 5) : []); }, [ids]);
  const loadOnMount = options.loadOnMount === true;
  useEffect(() => {
    if (!loadOnMount) return;
    let active = true;
    // The node keeps the last check; show it, but never replace a fresher result from a check started meanwhile.
    friendsApi.diagnostics().then((last) => { if (active) setResult((current) => current ?? last); }).catch(() => undefined);
    return () => { active = false; };
  }, [loadOnMount]);

  const act = async (operation: () => Promise<void>) => {
    if (busy) return;
    setBusy(true); setError(""); setNotice("");
    try { await operation(); }
    catch (cause) { setError(errorText(cause, tr("Connection checks could not be completed. Retry."))); }
    finally { setBusy(false); }
  };
  return {
    selected, setSelected, result, busy, error, notice, setNotice, act,
    check: (relationshipIds: string[]) => act(async () => { setResult(await friendsApi.diagnose(relationshipIds)); }),
    refreshStatus: () => act(async () => { setResult(await friendsApi.diagnostics()); }),
    retryDeliveries: (peerId: string) => act(async () => {
      await friendsApi.retry(peerId);
      setNotice(tr("Queued delivery retry completed. Open the conversation to check its delivery receipts."));
    }),
  };
}
export type FriendDiagnosticsController = ReturnType<typeof useFriendDiagnostics>;
