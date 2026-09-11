import { useCallback, useEffect, useRef, useState } from "react";
import { useAppContext } from "../../appContext";
import { Button } from "../../components/ui";
import { ReadingCleanupError, readingCleanup, type ReadingBackupReview, type ReadingCleanupJob, type ReadingCleanupReview, type ReadingCleanupStep } from "../../domain/readingCleanup";
import styles from "./ConversationCleanup.module.css";

const labels: Record<ReadingCleanupStep, string> = { source: "Reading history and bookmarks", replica: "Local sync copies", backups: "Reviewed backups", search: "Search index" };
const scope = "Clears reviewed reading history, bookmarks and progress, their local sync records, known migration backups and interrupted-write files, and rebuilds search. Shared sync/search backups may also contain other metadata. Saved documents, downloaded articles, browser copies and other devices are outside this cleanup. New work after the source cleanup remains. Unseen edits on another device may later require conflict review.";

export default function ReadingCleanupPanel({ onChange }: { onChange?: () => void }) {
  const { confirm } = useAppContext();
  const [job, setJob] = useState<ReadingCleanupJob | null>(null);
  const [review, setReview] = useState<ReadingCleanupReview | null>(null);
  const [attempt, setAttempt] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const mounted = useRef(true);
  const running = useRef(false);
  const statusRef = useRef<HTMLParagraphElement>(null);
  const reviewRef = useRef<HTMLDivElement>(null);
  const refresh = useCallback(async () => {
    const current = await readingCleanup.status();
    if (mounted.current) {
      setJob(current); setLoaded(true);
      setAttempt((previous) => current?.id === previous ? null : previous);
      if (current?.done.includes('source')) onChange?.();
    }
  }, [onChange]);
  const run = async (operation: () => Promise<void>) => {
    if (running.current) return;
    running.current = true;
    setBusy(true); setError(""); setNotice("");
    try { await operation(); }
    catch (cause) {
      if (mounted.current) {
        setError(cause instanceof Error ? cause.message : "Reading cleanup is unfinished. Refresh progress and retry.");
        if (cause instanceof ReadingCleanupError && ["reading_privacy_review_changed", "reading_cleanup_pending"].includes(cause.code)) {
          setReview(null); setAttempt(null);
        }
      }
      try { await refresh(); } catch { /* Retain the last confirmed progress. */ }
    } finally { running.current = false; if (mounted.current) setBusy(false); }
  };
  useEffect(() => {
    mounted.current = true;
    void run(refresh);
    return () => { mounted.current = false; };
  }, [refresh]);
  useEffect(() => { if (error || notice) statusRef.current?.focus(); }, [error, notice]);
  useEffect(() => { if (review && !busy && !error) reviewRef.current?.focus(); }, [review, busy, error]);
  const accept = (value: ReadingCleanupJob) => {
    if (!mounted.current) return;
    setJob(value); setAttempt(null); setReview(null);
    if (value.done.includes('source')) onChange?.();
    setNotice(value.cancelled ? "Uncommitted reading cleanup cancelled."
      : value.local_copies_complete ? "Reviewed reading copies on this node cleared. Other devices and saved or downloaded content remain outside this result."
        : "Reading cleanup is unfinished. Continue the remaining steps.");
  };
  const begin = (value: ReadingCleanupReview) => confirm({
    title: "Clear reviewed reading data?", risk: "high", confirmLabel: "Clear reviewed reading copies",
    body: `Local items: ${value.local_items}. Sync entries: ${value.source_entities}. Replica entries: ${value.replica_entities}. Backup files: ${value.backup_files}. ${scope}`,
    onConfirm: () => run(async () => { setAttempt(value.review_token); await readingCleanup.begin(value.review_token).then(accept); }),
  });
  const reviewBackups = async (current: ReadingCleanupJob) => {
    let backup: ReadingBackupReview | undefined;
    await run(async () => { backup = await readingCleanup.reviewBackups(current.id); });
    if (!backup || !mounted.current) return;
    const value = backup;
    confirm({ title: "Clear the remaining reading backups?", risk: "high", confirmLabel: "Clear reviewed reading backups",
      body: `Files: ${value.files}. Size: ${(value.bytes / 1024).toFixed(1)} KiB. These versions may differ from the original review. New reading records and unreviewed files remain.`,
      onConfirm: () => run(async () => { await readingCleanup.approveBackups(current.id, value.review_token).then(accept); }),
    });
  };
  const pending = job && !job.cancelled && !job.local_copies_complete;
  return <section className={styles.panel} aria-labelledby="reading-cleanup-title">
    <h3 id="reading-cleanup-title">Reading data</h3><p>{scope}</p>
    <p ref={statusRef} tabIndex={-1} role={error ? "alert" : "status"}>{error || notice}</p>
    <div className="button-row">
      <Button disabled={busy} onClick={() => void run(refresh)}>Refresh reading cleanup</Button>
      <Button disabled={busy || !loaded || Boolean(pending) || Boolean(attempt)} onClick={() => void run(async () => { const value = await readingCleanup.preview(); if (mounted.current) setReview(value); })}>Review reading data</Button>
    </div>
    {!loaded ? <p>Inspecting reading cleanup…</p> : null}
    {review && !pending && !attempt ? <div className={styles.review} ref={reviewRef} tabIndex={-1} aria-label="Reviewed reading scope">
      <p>Local items: {review.local_items} · Sync entries: {review.source_entities} · Replica entries: {review.replica_entities} · Backup files: {review.backup_files}</p>
      <Button disabled={busy} variant="danger" onClick={() => begin(review)}>Clear reviewed reading copies</Button>
    </div> : null}
    {attempt ? <Button disabled={busy} onClick={() => void run(async () => { await readingCleanup.begin(attempt).then(accept); })}>Retry original reading cleanup</Button> : null}
    {job ? <div>
      <h4>Latest reading cleanup {job.sequence}</h4>
      <p>{job.cancelled ? "Cancelled before source cleanup" : job.local_copies_complete ? "Reviewed local reading copies cleared" : "Reading cleanup unfinished"}</p>
      {job.done.length ? <p>Completed: {job.done.map((step) => labels[step]).join(", ")}</p> : null}
      {job.pending.length ? <p>Remaining: {job.pending.map((step) => labels[step]).join(", ")}</p> : null}
      {pending ? <div className="button-row">
        <Button disabled={busy} onClick={() => void run(async () => { await readingCleanup.resume(job.id).then(accept); })}>Continue reading cleanup</Button>
        {!job.done.length ? <Button disabled={busy} onClick={() => void run(async () => { await readingCleanup.cancel(job.id).then(accept); })}>Cancel uncommitted reading cleanup</Button> : null}
        {job.pending[0] === "backups" ? <Button disabled={busy} onClick={() => void reviewBackups(job)}>Review remaining reading backups</Button> : null}
      </div> : null}
    </div> : loaded ? <p>No previous reading cleanup.</p> : null}
  </section>;
}
