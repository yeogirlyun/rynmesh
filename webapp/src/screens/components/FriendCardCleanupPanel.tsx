import { useEffect, useRef, useState } from "react";
import { friendsApi } from "../../domain/friendsClient";
import { tr, useUILanguage } from "../../uiI18n";
import { errorText } from "./friendLinks";
import "../friends-refresh.css";

type Review = Awaited<ReturnType<typeof friendsApi.reviewCardCleanup>>;

export default function FriendCardCleanupPanel({ onChanged }: { onChanged: () => void }) {
  useUILanguage();
  const [review, setReview] = useState<Review | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const feedback = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLSpanElement>(null);
  useEffect(() => { if (!busy && (review || error || notice)) feedback.current?.focus(); }, [review, busy, error, notice]);
  const inspect = async () => {
    setBusy(true); setError(""); setNotice("");
    try { setReview(await friendsApi.reviewCardCleanup()); }
    catch (cause) { setError(errorText(cause, tr("Could not review card history. Retry."))); }
    finally { setBusy(false); }
  };
  const clear = async () => {
    if (!review) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await friendsApi.clearCards(review.review_token);
      if (!result.complete) throw new Error(tr("Card cleanup is unfinished. Retry the reviewed operation."));
      setReview(null);
      setNotice(tr("{{count}} reviewed card entries cleared locally.", { count: result.cards }) + " " + (result.remote_confirmed ? tr("Remote devices confirmed.") : tr("Other devices are not confirmed cleared.")));
      onChanged();
    } catch (cause) {
      setError(errorText(cause, tr("Card cleanup was not confirmed. Retry the same operation.")));
      onChanged();
    } finally { setBusy(false); }
  };
  return <section className="rq-cleanup" aria-label={tr("Card history maintenance")}>
    <h3>{tr("Clear card history")}</h3>
    <p>{tr("This clears reviewed sharing-card metadata and matching entries in known legacy files. Saved documents, reading records, messages, friendships and AI permissions remain. Use their own data controls to manage them. Deliveries already in flight or already received by someone else cannot be recalled.")}</p>
    <p>{tr("Only card identifiers and bounded cleanup receipts are retained to prevent old deliveries or retries from restoring cleared cards. Up to 10,000 deletion identifiers are retained; they are excluded from ordinary exports.")}</p>
    <span ref={trigger}><button type="button" className="rf-btn rf-btn-sm" disabled={busy} onClick={() => void inspect()}>{tr("Review card history cleanup")}</button></span>
    {review || error || notice ? <div ref={feedback} tabIndex={-1} className="rq-cleanup-result" role={error ? "alert" : notice ? "status" : "region"} aria-label={tr("Card cleanup review and result")}>
      {error ? <p>{error}</p> : null}
      {notice ? <p>{notice}</p> : null}
      {review ? <>
        <p>{tr("{{cards}} card entries · {{files}} legacy files.", { cards: review.cards, files: review.legacy_files })} {review.resuming ? tr("Continue the unfinished cleanup; cards created afterwards are kept.") : tr("Review this local scope before confirming.")}</p>
        <div className="rq-actions">
          <button type="button" className="rf-btn rf-btn-d rf-btn-sm" disabled={busy || review.cards === 0} onClick={() => void clear()}>{error ? tr("Retry reviewed card cleanup") : tr("Clear reviewed card history")}</button>
          <button type="button" className="rf-btn rf-btn-sm" disabled={busy} onClick={() => { setReview(null); setError(""); trigger.current?.querySelector("button")?.focus(); }}>{tr("Cancel review")}</button>
        </div>
      </> : null}
    </div> : null}
  </section>;
}
